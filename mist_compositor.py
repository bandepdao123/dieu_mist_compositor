"""Original white-mist compositor; stdlib API and FFmpeg process boundary."""
from __future__ import annotations

import argparse
from fractions import Fraction
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import tempfile

__version__ = '0.1.0'


def _run(command: list[str]) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(command, check=True, capture_output=True, text=True)
    except FileNotFoundError as exc:
        raise ValueError(f'Required executable not found: {command[0]}') from exc
    except subprocess.CalledProcessError as exc:
        raise ValueError(f'{command[0]} failed: {exc.stderr[-6000:]}') from exc


def _intensity(value: float) -> float:
    value = float(value)
    if not math.isfinite(value) or not 0 <= value <= 100:
        raise ValueError('intensity must be finite and in [0, 100]')
    return value


def build_filtergraph(width: int, height: int, fps: str, duration: float,
                      intensity: float) -> str:
    """Return graph consuming 0:v:0 and (unless zero) looped 1:v:0 -> [outv].

    Caller must supply SDR BT.709/assumed BT.709 inputs, disable autorotation,
    loop input 1, and enforce output duration. No paths or untrusted labels
    are interpolated into the graph. This is a generic OpenMontage contract,
    not a dependency on or a verified integration with OpenMontage.
    """
    strength = _intensity(intensity) / 100
    try:
        rate = Fraction(str(fps))
    except (ValueError, ZeroDivisionError) as exc:
        raise ValueError('fps must be a positive rational') from exc
    if (not isinstance(width, int) or not isinstance(height, int)
            or width <= 0 or height <= 0 or width % 2 or height % 2):
        raise ValueError('H264 yuv420p requires positive even dimensions')
    if rate <= 0 or not math.isfinite(duration) or duration <= 0:
        raise ValueError('fps and duration must be positive and finite')
    r = f'{rate.numerator}/{rate.denominator}'
    d = f'{duration:.9f}'
    base = (f'[0:v:0]setpts=PTS-STARTPTS,fps={r},trim=duration={d},'
            'scale=in_color_matrix=bt709:out_color_matrix=bt709:out_range=tv,'
            'format=yuv420p')
    if strength == 0:
        return base + '[outv]'
    return ';'.join([
        base + '[base]',
        f'[1:v:0]setpts=PTS-STARTPTS,scale={width}:{height}:out_range=full,'
        f'format=gray,fps={r},trim=duration={d},setsar=1,'
        f"lut=y='val*{strength:.9f}'[alpha]",
        f'color=c=white:s={width}x{height}:r={r}:d={d},format=yuv420p[white]',
        '[white][alpha]alphamerge[veil]',
        '[base][veil]overlay=shortest=1:format=yuv420,format=yuv420p[outv]',
    ])


def probe(path: str | Path) -> dict:
    """Probe a local, readable video file and reject unsupported color modes."""
    path = Path(path).expanduser().resolve()
    if not path.is_file():
        raise ValueError(f'Not a local file: {path}')
    data = json.loads(_run(['ffprobe', '-v', 'error', '-show_streams',
                           '-show_format', '-of', 'json', str(path)]).stdout)
    videos = [s for s in data.get('streams', []) if s.get('codec_type') == 'video'
              and not s.get('disposition', {}).get('attached_pic')]
    if not videos:
        raise ValueError(f'No video stream: {path}')
    # Bound the supported color contract instead of silently relabeling HDR.
    video = videos[0]
    for field, allowed in [('color_transfer', {'unknown', 'bt709'}),
                           ('color_primaries', {'unknown', 'bt709'}),
                           ('color_space', {'unknown', 'bt709', 'gbr'})]:
        if video.get(field, 'unknown') not in allowed:
            raise ValueError(f'Unsupported HDR/non-BT709 SDR: {field}={video[field]}')
    for side in video.get('side_data_list', []):
        label = side.get('side_data_type', '').lower()
        if any(word in label for word in ('mastering', 'content light', 'dovi', 'hdr')):
            raise ValueError('HDR metadata is not supported; tone-map externally first')
        if side.get('rotation', 0):
            raise ValueError('Rotated video unsupported; normalize orientation first')
    if video.get('tags', {}).get('rotate', '0') not in ('0', 0):
        raise ValueError('Rotated video unsupported; normalize orientation first')
    if video.get('sample_aspect_ratio', '1:1') not in ('1:1', 'N/A', '0:1'):
        raise ValueError('Non-square pixels unsupported; normalize aspect ratio first')
    return data


def _video(data: dict) -> dict:
    return next(s for s in data['streams'] if s.get('codec_type') == 'video')


def _timing(data: dict) -> tuple[str, float]:
    video = _video(data)
    try:
        fps = Fraction(video.get('avg_frame_rate', '0/1'))
        duration = float(video.get('duration', data.get('format', {}).get('duration', 0)))
    except (ValueError, ZeroDivisionError, TypeError) as exc:
        raise ValueError('Missing or invalid video timing') from exc
    if fps <= 0 or not math.isfinite(duration) or duration <= 0:
        raise ValueError('Missing positive video duration/frame rate')
    return f'{fps.numerator}/{fps.denominator}', duration


def render(input: str | Path, output: str | Path, *, intensity: float = 50,
           mist: str | Path | None = None, overwrite: bool = False) -> dict:
    """Render atomically to MP4. Returns probes, actual argv, and verified checks.

    Existing destinations are refused atomically unless overwrite=True. On any
    encode/probe/check failure, the old destination remains untouched. Audio
    streams are re-encoded to AAC, with their timing relative to video retained.
    """
    intensity = _intensity(intensity)
    source = Path(input).expanduser().resolve()
    target = Path(output).expanduser().absolute()
    if target.suffix.lower() != '.mp4':
        raise ValueError('Output must have .mp4 extension')
    if target.resolve() == source or (mist and target.resolve() == Path(mist).expanduser().resolve()):
        raise ValueError('Output must not replace an input asset')
    if os.path.lexists(target) and not overwrite:
        raise FileExistsError(f'Output exists: {target}')
    if not target.parent.is_dir():
        raise ValueError('Output parent directory must exist')
    source_probe = probe(source)
    fps, duration = _timing(source_probe)
    video = _video(source_probe)
    graph = build_filtergraph(video['width'], video['height'], fps, duration, intensity)
    mist_probe = None
    if intensity:
        if mist is None:
            raise ValueError('--mist is required when intensity > 0')
        mist = Path(mist).expanduser().resolve()
        mist_probe = probe(mist)
        _timing(mist_probe)
    fd, temporary = tempfile.mkstemp(prefix='.mist-', suffix='.mp4', dir=target.parent)
    os.close(fd)
    try:
        command = ['ffmpeg', '-hide_banner', '-v', 'error', '-nostdin', '-y',
                   '-filter_complex_threads', '1', '-copyts', '-noautorotate', '-i', str(source)]
        if intensity:
            command += ['-stream_loop', '-1', '-noautorotate', '-i', str(mist)]
        command += ['-filter_complex', graph, '-map', '[outv]', '-map', '0:a?',
                    '-map_metadata', '-1', '-map_chapters', '-1',
                    '-c:v', 'libx264', '-threads', '1', '-preset', 'medium', '-crf', '18',
                    '-pix_fmt', 'yuv420p', '-color_primaries', 'bt709', '-color_trc', 'bt709',
                    '-colorspace', 'bt709', '-color_range', 'tv', '-c:a', 'aac', '-b:a', '192k']
        if any(s.get('codec_type') == 'audio' for s in source_probe['streams']):
            start = float(video.get('start_time', 0))
            if not math.isfinite(start):
                raise ValueError('Invalid source start timestamp')
            command += ['-af', f'asetpts=PTS-({start:.9f})/TB,atrim=start=0,apad']
        command += ['-t', f'{duration:.9f}', '-movflags', '+faststart', temporary]
        _run(command)
        output_probe = probe(temporary)
        out_fps, out_duration = _timing(output_probe)
        out_video = _video(output_probe)
        tolerance = max(0.05, 2 / float(Fraction(fps)))
        audio_count = lambda p: sum(s.get('codec_type') == 'audio' for s in p['streams'])
        checks = {
            'source_duration': duration,
            'output_duration': out_duration,
            'duration_error': abs(out_duration - duration),
            'duration_tolerance': tolerance,
            'duration_ok': abs(out_duration - duration) <= tolerance,
            'container_duration_ok': abs(float(output_probe['format']['duration']) - duration) <= tolerance,
            'audio_duration_ok': all(
                abs(float(s.get('start_time', 0)) + float(s.get('duration', 0)) - duration) <= tolerance
                for s in output_probe['streams'] if s.get('codec_type') == 'audio'),
            'fps_ok': Fraction(out_fps) == Fraction(fps),
            'dimensions_ok': (out_video['width'], out_video['height']) == (video['width'], video['height']),
            'audio_streams_ok': audio_count(source_probe) == audio_count(output_probe),
            'encoding_ok': out_video.get('codec_name') == 'h264' and out_video.get('pix_fmt') == 'yuv420p',
        }
        checks['passed'] = all(value for key, value in checks.items() if key.endswith('_ok'))
        if not checks['passed']:
            raise ValueError(f'Output verification failed: {checks}')
        if overwrite:
            os.replace(temporary, target)
        else:
            # Hard-link creation is atomic and cannot clobber a racing writer.
            os.link(temporary, target)
        output_probe['format']['filename'] = str(target)
        return {'schema_version': 1, 'input_probe': source_probe, 'mist_probe': mist_probe,
                'output_probe': output_probe, 'command': command, 'filtergraph': graph,
                'checks': checks, 'output': str(target),
                'color_policy': 'BT709 SDR only; untagged input assumed BT709 SDR',
                'audio_policy': 'all source audio streams mapped optionally; AAC 192k, not bit-exact'}
    finally:
        Path(temporary).unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='action', required=True)
    cmd = commands.add_parser('render', help='Render white mist to BT709 SDR MP4')
    cmd.add_argument('--input', required=True)
    cmd.add_argument('--mist')
    cmd.add_argument('--intensity', required=True, type=float)
    cmd.add_argument('--output', required=True)
    cmd.add_argument('--overwrite', action='store_true')
    args = parser.parse_args(argv)
    try:
        report = render(args.input, args.output, intensity=args.intensity,
                        mist=args.mist, overwrite=args.overwrite)
    except (ValueError, OSError) as exc:
        print(json.dumps({'error': str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    sys.exit(main())
