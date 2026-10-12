package dev.homeai.androidinput;

import android.app.Activity;
import android.os.Bundle;
import android.graphics.Color;
import android.view.MotionEvent;
import android.view.View;
import android.view.WindowManager;
import android.view.GestureDetector;
import android.view.ScaleGestureDetector;
import android.widget.LinearLayout;
import android.widget.TextView;

/** 仅用于真机公开手势验收，不读取任何应用数据，不持久化触点。 */
public final class TouchTestActivity extends Activity {
    private TextView status;
    private int downs, ups, moves, pointerDowns, pointerUps, cancels, peak;
    private long duration;
    private final StringBuilder timing = new StringBuilder();
    private long timingStart;
    private int longPresses, doubleTaps, scales;
    private GestureDetector gestures;
    private ScaleGestureDetector scaling;
    @Override public void onCreate(Bundle state) {
        super.onCreate(state);
        getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
        gestures = new GestureDetector(this, new GestureDetector.SimpleOnGestureListener() {
            @Override public boolean onDown(MotionEvent event) { return true; }
            @Override public void onLongPress(MotionEvent event) { longPresses++; showStatus(); }
            @Override public boolean onDoubleTap(MotionEvent event) { doubleTaps++; showStatus(); return true; }
        });
        scaling = new ScaleGestureDetector(this, new ScaleGestureDetector.SimpleOnScaleGestureListener() {
            @Override public boolean onScale(ScaleGestureDetector detector) { scales++; showStatus(); return true; }
        });
        LinearLayout layout = new LinearLayout(this);
        layout.setOrientation(LinearLayout.VERTICAL);
        layout.setBackgroundColor(Color.rgb(15, 36, 48));
        TextView title = new TextView(this);
        title.setText("Home AI 手势验收\n公开测试区域，不操作其他应用");
        title.setTextColor(Color.WHITE); title.setTextSize(20); title.setPadding(24,24,24,24);
        layout.addView(title);
        status = new TextView(this);
        status.setTextColor(Color.CYAN); status.setTextSize(16); status.setPadding(24,24,24,24);
        // 固定计数区域高度，避免更新文字换行时移动测试触控区域。
        layout.addView(status, new LinearLayout.LayoutParams(-1,
            (int)(220 * getResources().getDisplayMetrics().density)));
        View surface = new View(this) {
            @Override public boolean onTouchEvent(MotionEvent event) {
                gestures.onTouchEvent(event); scaling.onTouchEvent(event);
                int action = event.getActionMasked();
                if (action == MotionEvent.ACTION_DOWN || action == MotionEvent.ACTION_UP || action == MotionEvent.ACTION_CANCEL) {
                    if (timingStart == 0) timingStart = event.getEventTime();
                    if (timing.length() < 240) timing.append(action).append(':').append(event.getEventTime()-timingStart).append(' ');
                }
                peak = Math.max(peak, event.getPointerCount());
                if (action == MotionEvent.ACTION_DOWN) downs++;
                if (action == MotionEvent.ACTION_UP) { ups++; duration=event.getEventTime()-event.getDownTime(); }
                if (action == MotionEvent.ACTION_MOVE) moves++;
                if (action == MotionEvent.ACTION_POINTER_DOWN) pointerDowns++;
                if (action == MotionEvent.ACTION_POINTER_UP) pointerUps++;
                if (action == MotionEvent.ACTION_CANCEL) cancels++;
                showStatus();
                return true;
            }
        };
        surface.setBackgroundColor(Color.rgb(18, 110, 124));
        surface.setContentDescription("gesture-test-surface");
        // 向无障碍树标明真实可点击区域，供 Core 按控件身份验证公开自动操作。
        surface.setClickable(true);
        layout.addView(surface, new LinearLayout.LayoutParams(-1, 0, 1));
        setContentView(layout); showStatus();
    }
    private void showStatus() {
        status.setText("TOUCH_TEST down="+downs+" up="+ups+" move="+moves+" pointer_down="+pointerDowns+
            " pointer_up="+pointerUps+" cancel="+cancels+" peak="+peak+" duration_ms="+duration+
            " long_press="+longPresses+" double_tap="+doubleTaps+" scale="+scales+" times="+timing);
    }
}
