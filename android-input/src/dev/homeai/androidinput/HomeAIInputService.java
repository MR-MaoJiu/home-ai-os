package dev.homeai.androidinput;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.content.IntentFilter;
import android.inputmethodservice.InputMethodService;
import android.os.Build;
import android.util.Base64;
import android.view.View;
import android.view.inputmethod.InputConnection;
import android.view.inputmethod.InputMethodManager;
import android.widget.Button;
import java.nio.charset.StandardCharsets;

/** 不联网、不读取已有文字，仅接收已授权调试端提交的 UTF-8 输入。 */
public final class HomeAIInputService extends InputMethodService {
    private final BroadcastReceiver receiver = new BroadcastReceiver() {
        @Override public void onReceive(Context context, Intent intent) {
            setResultCode(0);
            if (!"dev.homeai.input.COMMIT".equals(intent.getAction())) return;
            String encoded = intent.getStringExtra("text_base64");
            if (encoded == null || encoded.length() > 8192) return;
            try {
                String value = new String(Base64.decode(encoded, Base64.NO_WRAP), StandardCharsets.UTF_8);
                InputConnection target = getCurrentInputConnection();
                if (target != null && value.length() > 0 && value.length() <= 1000 && target.commitText(value, 1)) {
                    setResultCode(1);
                }
            } catch (IllegalArgumentException ignored) { }
        }
    };

    @Override public void onCreate() {
        super.onCreate();
        IntentFilter filter = new IntentFilter("dev.homeai.input.COMMIT");
        // DUMP 属于受保护的系统权限；普通第三方 App 不能向此入口注入文字。
        if (Build.VERSION.SDK_INT >= 33) registerReceiver(receiver, filter, "android.permission.DUMP", null, Context.RECEIVER_EXPORTED);
        else registerReceiver(receiver, filter, "android.permission.DUMP", null);
    }

    @Override public View onCreateInputView() {
        Button switcher = new Button(this);
        switcher.setText("Home AI 远程输入 · 点击切换回手机键盘");
        switcher.setOnClickListener(new View.OnClickListener() {
            @Override public void onClick(View view) {
                ((InputMethodManager)getSystemService(INPUT_METHOD_SERVICE)).showInputMethodPicker();
            }
        });
        return switcher;
    }

    @Override public void onDestroy() {
        unregisterReceiver(receiver);
        super.onDestroy();
    }
}
