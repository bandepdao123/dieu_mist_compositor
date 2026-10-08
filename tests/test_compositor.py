import array
import json
import math
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import mist_compositor as mc


def ff(*args):
    return subprocess.run(['ffmpeg', '-v', 'error', '-threads', '1', '-filter_threads', '1', '-y', *map(str, args)], check=True, capture_output=True).stdout


class UnitTests(unittest.TestCase):
    def test_range(self):
        for value in [-1, 101, float('nan'), float('inf')]:
            with self.assertRaises(ValueError):
                mc.build_filtergraph(64, 48, '30000/1001', 1, value)

    def test_graph_contract(self):
        graph = mc.build_filtergraph(64, 48, '30000/1001', 1, 50)
        for text in ['color=c=white', 'format=gray', 'alphamerge', 'overlay', '30000/1001', 'PTS-STARTPTS']:
            self.assertIn(text, graph)
        self.assertNotIn('screen', graph)
        self.assertNotIn('1:v', mc.build_filtergraph(64, 48, '25/1', 1, 0))

    def test_invalid_geometry(self):
        for args in [(63,48,'25',1,50), (64,48,'0/0',1,50), (64,48,'25',-1,50)]:
            with self.assertRaises(ValueError):
                mc.build_filtergraph(*args)


class IntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.tmp.name)
        cls.source = cls.root / 'source.mp4'
        cls.silent = cls.root / 'silent.mp4'
        # Entirely generated neutral background; rational frame rate.
        ff('-f','lavfi','-i','color=c=0x202020:s=64x48:r=30000/1001:d=1.2012', '-f','lavfi','-i','sine=frequency=440:sample_rate=48000:duration=1.2012', '-c:v','libx264','-pix_fmt','yuv420p','-color_primaries','bt709','-color_trc','bt709','-colorspace','bt709','-c:a','aac',cls.source)
        ff('-i',cls.source,'-an','-c:v','copy',cls.silent)
        cls.mist = cls.root / 'mist.mkv'
        ff('-f','lavfi','-i',"nullsrc=s=64x48:r=15:d=0.2,geq=lum='if(lt(X,21),0,if(lt(X,42),128,255))':cb=128:cr=128,format=gray",'-c:v','ffv1',cls.mist)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def pixels(self, path, time):
        raw = ff('-ss',time,'-i',path,'-frames:v','1','-f','rawvideo','-pix_fmt','rgb24','pipe:1')
        return [tuple(raw[(24*64+x)*3:(24*64+x)*3+3]) for x in (8,32,54)]

    def test_matrix_pixels_duration_audio(self):
        for audio, source in [(True,self.source),(False,self.silent)]:
            outputs = {}
            for intensity in (0,50,100):
                with self.subTest(audio=audio,intensity=intensity):
                    out = self.root / f'render-{audio}-{intensity}.mp4'
                    report = mc.render(source, out, intensity=intensity, mist=self.mist if intensity else None)
                    outputs[intensity] = out
                    self.assertTrue(report['checks']['passed'])
                    self.assertIn('command',report)
                    video = next(s for s in report['output_probe']['streams'] if s['codec_type']=='video')
                    self.assertEqual((video['width'],video['height']),(64,48))
                    self.assertEqual(video['avg_frame_rate'],'30000/1001')
                    self.assertEqual(video['color_transfer'],'bt709')
                    self.assertEqual(video['pix_fmt'],'yuv420p')
                    self.assertEqual(any(s['codec_type']=='audio' for s in report['output_probe']['streams']),audio)
                    # Late frame proves short mask actually loops throughout source.
                    for time in ('0.1','1.0'):
                        pixels = self.pixels(out,time)
                        for rgb in pixels:
                            self.assertLessEqual(max(rgb)-min(rgb),4, rgb)
                        expected = [32,32,32] if intensity==0 else ([32,88,144] if intensity==50 else [32,144,255])
                        for rgb, target in zip(pixels,expected):
                            self.assertAlmostEqual(sum(rgb)/3,target,delta=9)
                    if audio:
                        raw = ff('-i',out,'-vn','-ac','1','-ar','48000','-f','f32le','pipe:1')
                        samples=array.array('f',raw)
                        reference=array.array('f',ff('-i',source,'-vn','-ac','1','-ar','48000','-f','f32le','pipe:1'))
                        pairs=list(zip(samples[:48000],reference[:48000]))
                        corr=sum(a*b for a,b in pairs)/math.sqrt(sum(a*a for a,b in pairs)*sum(b*b for a,b in pairs))
                        self.assertGreater(corr,0.98)
                        self.assertGreater(math.sqrt(sum(a*a for a in samples)/len(samples)),0.05)

    def test_refuse_overwrite_and_fail_clean(self):
        out=self.root/'existing.mp4'
        out.write_bytes(b'keep')
        with self.assertRaises(FileExistsError):
            mc.render(self.source,out,intensity=0)
        self.assertEqual(out.read_bytes(),b'keep')
        bad=self.root/'bad.mp4'
        bad.write_text('not video')
        for source,mist in [(bad,None),(self.source,bad),(self.source,None)]:
            with self.assertRaises(ValueError):
                mc.render(source,self.root/'failed.mp4',intensity=50,mist=mist)
            self.assertFalse((self.root/'failed.mp4').exists())
        self.assertFalse(list(self.root.glob('.mist-*')))

    def test_hdr_rejected(self):
        hdr=self.root/'hdr.mp4'
        ff('-i',self.silent,'-c:v','libx264','-color_trc','smpte2084','-color_primaries','bt2020','-colorspace','bt2020nc',hdr)
        with self.assertRaisesRegex(ValueError,'HDR|SDR'):
            mc.render(hdr,self.root/'hdr-out.mp4',intensity=0)

    def test_short_audio_padded(self):
        source=self.root/'short-audio.mp4'
        ff('-i',self.silent,'-f','lavfi','-i','sine=frequency=440:duration=0.2',
           '-map','0:v','-map','1:a','-c:v','copy','-c:a','aac',source)
        report=mc.render(source,self.root/'short-audio-out.mp4',intensity=0)
        self.assertTrue(report['checks']['audio_duration_ok'])
        self.assertTrue(report['checks']['container_duration_ok'])
        audio=next(s for s in report['output_probe']['streams'] if s['codec_type']=='audio')
        self.assertAlmostEqual(float(audio['duration']),report['checks']['source_duration'],delta=0.06)

    def test_offset_timestamps(self):
        shifted=self.root/'shifted.mp4'
        ff('-i',self.source,'-c','copy','-output_ts_offset','5',shifted)
        out=self.root/'shifted-out.mp4'
        report=mc.render(shifted,out,intensity=50,mist=self.mist)
        self.assertTrue(report['checks']['passed'])
        video=next(s for s in report['output_probe']['streams'] if s['codec_type']=='video')
        audio=next(s for s in report['output_probe']['streams'] if s['codec_type']=='audio')
        self.assertAlmostEqual(float(video['start_time']),0,delta=0.04)
        self.assertAlmostEqual(float(audio['start_time']),0,delta=0.04)
        self.assertAlmostEqual(float(audio['duration']),float(video['duration']),delta=0.06)

    def test_encoder_failure_preserves_old_output(self):
        from unittest.mock import patch
        out=self.root/'preserved.mp4'
        out.write_bytes(b'old output')
        real_run=mc._run
        def fail_encode(command):
            if command[0]=='ffmpeg':
                Path(command[-1]).write_bytes(b'partial')
                raise ValueError('simulated encoder failure after partial write')
            return real_run(command)
        with patch.object(mc,'_run',side_effect=fail_encode):
            with self.assertRaisesRegex(ValueError,'encoder failure'):
                mc.render(self.source,out,intensity=0,overwrite=True)
        self.assertEqual(out.read_bytes(),b'old output')
        self.assertFalse(list(self.root.glob('.mist-*')))

    def test_cli_invalid_range(self):
        out=self.root/'invalid-range.mp4'
        run=subprocess.run([sys.executable,'-m','mist_compositor','render','--input',str(self.source),'--intensity','101','--output',str(out)],capture_output=True,text=True)
        self.assertEqual(run.returncode,1)
        self.assertIn('error',json.loads(run.stderr))
        self.assertFalse(out.exists())

    def test_cli_json(self):
        out=self.root/'cli.mp4'
        run=subprocess.run([sys.executable,'-m','mist_compositor','render','--input',str(self.silent),'--intensity','0','--output',str(out)],capture_output=True,text=True)
        self.assertEqual(run.returncode,0,run.stderr)
        self.assertTrue(json.loads(run.stdout)['checks']['passed'])


if __name__ == '__main__':
    unittest.main()
