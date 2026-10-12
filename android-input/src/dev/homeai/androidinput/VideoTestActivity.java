package dev.homeai.androidinput;

import android.app.Activity;
import android.os.Bundle;
import android.os.SystemClock;
import android.graphics.Canvas;
import android.graphics.Color;
import android.graphics.Paint;
import android.view.View;
import android.view.WindowManager;
import android.media.AudioAttributes;
import android.media.AudioFormat;
import android.media.AudioTrack;

/** 公开动画与可选测试音，不访问账号、文件或麦克风，不保存媒体。 */
public final class VideoTestActivity extends Activity {
    private volatile boolean sounding;
    private Thread audioThread;
    @Override public void onCreate(Bundle state) {
        super.onCreate(state);
        getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
        setContentView(new View(this) {
            final Paint paint = new Paint(Paint.ANTI_ALIAS_FLAG);
            final long started = SystemClock.elapsedRealtime();
            int frames;
            @Override protected void onDraw(Canvas canvas) {
                canvas.drawColor(Color.rgb(12, 32, 45));
                float seconds = (SystemClock.elapsedRealtime() - started) / 1000f;
                float width = getWidth(), height = getHeight();
                paint.setColor(Color.WHITE); paint.setTextSize(width / 18);
                canvas.drawText("Home AI 视频验收", width * .07f, height * .12f, paint);
                paint.setTextSize(width / 26);
                canvas.drawText("公开动画 · 不操作其他应用", width * .07f, height * .17f, paint);
                canvas.drawText("FRAME " + (++frames), width * .07f, height * .23f, paint);
                float centerX = width / 2, centerY = height * .56f;
                for (int index = 0; index < 12; index++) {
                    double angle = seconds * 1.7 + index * Math.PI / 6;
                    paint.setColor(Color.HSVToColor(new float[]{index * 30, .8f, 1}));
                    canvas.drawCircle(centerX + (float)Math.cos(angle) * width * .3f,
                        centerY + (float)Math.sin(angle) * width * .3f, width * .035f, paint);
                }
                paint.setColor(Color.CYAN);
                float x = width * (.1f + .7f * (seconds % 2) / 2);
                canvas.drawRect(x, height * .83f, x + width * .1f, height * .86f, paint);
                postInvalidateOnAnimation();
            }
        });
        if (getIntent().getBooleanExtra("audio_test", false)) startPublicTone();
    }

    private void startPublicTone() {
        sounding = true;
        audioThread = new Thread(new Runnable() {
            @Override public void run() {
                AudioAttributes.Builder attributes = new AudioAttributes.Builder()
                    .setUsage(AudioAttributes.USAGE_MEDIA).setContentType(AudioAttributes.CONTENT_TYPE_MUSIC);
                if (android.os.Build.VERSION.SDK_INT >= 29)
                    attributes.setAllowedCapturePolicy(AudioAttributes.ALLOW_CAPTURE_BY_ALL);
                AudioTrack track = new AudioTrack.Builder().setAudioAttributes(attributes.build())
                    .setAudioFormat(new AudioFormat.Builder().setSampleRate(48000)
                        .setEncoding(AudioFormat.ENCODING_PCM_16BIT).setChannelMask(AudioFormat.CHANNEL_OUT_STEREO).build())
                    .setBufferSizeInBytes(19200).setTransferMode(AudioTrack.MODE_STREAM).build();
                try {
                    short[] samples = new short[960]; long position = 0;
                    track.play();
                    while (sounding) {
                        for (int index = 0; index < 480; index++, position++) {
                            samples[index * 2] = (short)(1200 * Math.sin(2 * Math.PI * 440 * position / 48000));
                            samples[index * 2 + 1] = (short)(1200 * Math.sin(2 * Math.PI * 660 * position / 48000));
                        }
                        if (track.write(samples, 0, samples.length) < 0) break;
                    }
                } finally { track.stop(); track.release(); }
            }
        }, "homeai-public-test-tone");
        audioThread.start();
    }

    @Override public void onPause() {
        sounding = false;
        super.onPause();
    }
}
