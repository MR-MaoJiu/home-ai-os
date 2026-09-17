"""生成中文合成语音测试输入；不生成或伪造 ASR 输出。仅适用于 macOS。"""
import shutil,subprocess
from pathlib import Path
root=Path(__file__).resolve().parents[1]
folder=root/'state/voice-fixtures';folder.mkdir(parents=True,exist_ok=True)
say=shutil.which('say');ffmpeg=shutil.which('ffmpeg')
if not say or not ffmpeg:raise SystemExit('此测试音频生成脚本需要 macOS say 与 ffmpeg；其他平台可提供同内容 PCM WAV')
subprocess.run([say,'-v','Tingting (中文（中国大陆）)','-o',str(folder/'chinese.aiff'),'请提醒我明天下午三点检查家庭服务器备份。'],check=True)
subprocess.run([ffmpeg,'-nostdin','-y','-i',str(folder/'chinese.aiff'),'-ar','16000','-ac','1','-c:a','pcm_s16le',str(folder/'chinese.wav')],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
print('已生成中文合成语音测试文件；它不代表真人或真机录音验收。')
