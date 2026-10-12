package dev.homeai.androidinput;

import android.os.Build;
import android.os.IBinder;
import android.os.SystemClock;
import org.json.JSONObject;
import java.io.BufferedReader;
import java.io.InputStreamReader;
import java.io.PrintWriter;
import java.lang.reflect.Method;
import java.nio.charset.StandardCharsets;
import java.util.HashSet;
import java.util.Iterator;
import java.util.Set;

/** 固定实体屏幕电源桥；不注入电源键、不解锁、不修改全局自动锁定配置。 */
public final class PowerServer {
    static final long LEASE_MS = 5000;
    static final long ACTIVITY_MS = 1000;
    static final int LOGICAL_ON = 2;
    static final int POWER_OFF = 0;
    static final int POWER_NORMAL = 2;
    static final int NO_CHANGE_LIGHTS = 1;

    interface Clock { long now(); }
    interface Backend {
        Snapshot snapshot() throws Exception;
        void power(int mode) throws Exception;
        void userActivity() throws Exception;
        void refreshLights() throws Exception;
    }
    static final class Snapshot {
        final int logicalState;
        final boolean interactive, locked;
        Snapshot(int state, boolean interactive, boolean locked) {
            this.logicalState = state; this.interactive = interactive; this.locked = locked;
        }
    }
    static final class Failure extends Exception {
        final String code;
        Failure(String code) { super(code); this.code = code; }
    }
    static final class Request {
        final long id;
        final String action;
        Request(long id, String action) { this.id = id; this.action = action; }
    }

    static Request validate(Set<String> keys, Object id, Object action) throws Failure {
        if (keys.size() != 2 || !keys.contains("id") || !keys.contains("action") ||
            !(id instanceof Integer || id instanceof Long) || !(action instanceof String)) throw new Failure("invalid_packet");
        long identifier = ((Number)id).longValue();
        String command = (String)action;
        if (identifier < 1 || !(command.equals("off") || command.equals("on") ||
            command.equals("status") || command.equals("keepalive"))) throw new Failure("invalid_packet");
        return new Request(identifier, command);
    }

    /** 状态机与 Android 反射隔离，便于在普通 JVM 验证恢复、超时和锁屏边界。 */
    static final class Controller {
        private final Backend backend;
        private final Clock clock;
        private long lastHeartbeat, lastActivity, lastId;
        private boolean requestedOff, restorePending, originalOn, finished;
        private String restoreReason = "";
        private Snapshot latest;

        Controller(Backend backend, Clock clock) throws Exception {
            this.backend = backend; this.clock = clock;
            latest = backend.snapshot();
            lastHeartbeat = clock.now();
        }

        synchronized boolean command(Request request) throws Exception {
            if (request.id <= lastId) throw new Failure("stale_request");
            if (finished) throw new Failure("lease_ended");
            if (restorePending && clock.now() - lastHeartbeat >= LEASE_MS) {
                finished = true;
                restore("heartbeat_timeout");
                throw new Failure("lease_expired");
            }
            lastId = request.id;
            latest = backend.snapshot();
            lastHeartbeat = clock.now();
            boolean applied = false;
            if (request.action.equals("off")) {
                if (latest.locked) throw new Failure("device_locked");
                if (!latest.interactive || latest.logicalState != LOGICAL_ON) throw new Failure("device_not_interactive");
                if (!requestedOff) {
                    originalOn = true;
                    // setter 返回异常也可能已改变显示，先登记恢复责任。
                    restorePending = true;
                    backend.power(POWER_OFF);
                    requestedOff = true;
                    restoreReason = "";
                    lastActivity = clock.now() - ACTIVITY_MS;
                    applied = true;
                }
            } else if (request.action.equals("on")) {
                applied = restorePending ? restore("requested") : refreshLights();
            }
            if (requestedOff && (latest.locked || !latest.interactive || latest.logicalState != LOGICAL_ON)) {
                finished = true;
                applied = restore(latest.locked ? "system_locked" : "system_sleep");
            }
            latest = backend.snapshot();
            return applied;
        }

        synchronized boolean tick() throws Exception {
            if (finished) return true;
            if (!restorePending) return false;
            if (clock.now() - lastHeartbeat >= LEASE_MS) {
                finished = true;
                restore("heartbeat_timeout");
                return true;
            }
            latest = backend.snapshot();
            if (latest.locked || !latest.interactive || latest.logicalState != LOGICAL_ON) {
                finished = true;
                restore(latest.locked ? "system_locked" : "system_sleep");
                return true;
            }
            if (requestedOff && clock.now() - lastActivity >= ACTIVITY_MS) {
                // 只延续当前已解锁的交互会话；NO_CHANGE_LIGHTS 不把实体屏幕重新点亮。
                backend.userActivity();
                lastActivity = clock.now();
            }
            return false;
        }

        synchronized boolean restore(String reason) throws Exception {
            if (!restorePending) return false;
            latest = backend.snapshot();
            boolean applied = false;
            if (originalOn && latest.interactive && latest.logicalState == LOGICAL_ON) {
                backend.power(POWER_NORMAL);
                applied = true;
                restoreReason = reason;
            } else {
                // 用户已让系统睡眠时保持系统决定，不发送唤醒或解锁动作。
                restoreReason = "system_sleep_preserved";
            }
            requestedOff = false;
            restorePending = false;
            if (applied) refreshLights();
            return applied;
        }

        private boolean refreshLights() throws Exception {
            // 恢复 NORMAL 后重新核对，不能把期间发生的系统锁屏或睡眠当作仍已解锁。
            latest = backend.snapshot();
            if (!latest.interactive || latest.logicalState != LOGICAL_ON || latest.locked) return false;
            backend.refreshLights();
            return true;
        }

        synchronized void finish(String reason) throws Exception {
            finished = true;
            restore(reason);
        }

        synchronized JSONObject reply(long id, boolean ok, boolean applied, String error) throws Exception {
            return new JSONObject().put("id", id).put("ok", ok).put("supported", true)
                .put("applied", applied).put("requested_off", requestedOff).put("restore_pending", restorePending)
                .put("physical_verified", false).put("physical_off", JSONObject.NULL)
                .put("interactive", latest.interactive).put("keyguard_locked", latest.locked)
                .put("logical_display_state", latest.logicalState).put("restore_reason", restoreReason)
                .put("error", error).put("lease_timeout_ms", LEASE_MS);
        }
    }

    /** 兼容路径参考 scrcpy v4.1 的 SurfaceControl、DisplayControl、PowerManager wrappers。 */
    private static final class AndroidBackend implements Backend {
        private final Object display, power, window;
        private final Method displayInfo, interactive, keyguard, activity, setPower;
        private final boolean interactiveHasDisplay, activityHasDisplay;
        private final IBinder token;

        AndroidBackend() throws Exception {
            display = service("display", "android.hardware.display.IDisplayManager");
            power = service("power", "android.os.IPowerManager");
            window = service("window", "android.view.IWindowManager");
            displayInfo = method(display, "getDisplayInfo", int.class);
            keyguard = method(window, "isKeyguardLocked");
            Method readInteractive;
            boolean perDisplay;
            try { readInteractive = method(power, "isDisplayInteractive", int.class); perDisplay = true; }
            catch (NoSuchMethodException missing) { readInteractive = method(power, "isInteractive"); perDisplay = false; }
            interactive = readInteractive; interactiveHasDisplay = perDisplay;
            Method touchActivity;
            boolean activityDisplay;
            try { touchActivity = method(power, "userActivity", int.class, long.class, int.class, int.class); activityDisplay = true; }
            catch (NoSuchMethodException missing) { touchActivity = method(power, "userActivity", long.class, int.class, int.class); activityDisplay = false; }
            activity = touchActivity; activityHasDisplay = activityDisplay;
            Class<?> surface = Class.forName("android.view.SurfaceControl");
            setPower = surface.getMethod("setDisplayPowerMode", IBinder.class, int.class);
            token = internalToken(surface);
            if (token == null) throw new Failure("physical_display_unavailable");
        }

        private static Object service(String name, String contract) throws Exception {
            Object binder = Class.forName("android.os.ServiceManager").getMethod("getService", String.class).invoke(null, name);
            if (binder == null) throw new Failure("system_service_unavailable");
            return Class.forName(contract + "$Stub").getMethod("asInterface", IBinder.class).invoke(null, binder);
        }

        private static Method method(Object target, String name, Class<?>... parameters) throws Exception {
            Method result = target.getClass().getMethod(name, parameters);
            result.setAccessible(true);
            return result;
        }

        private IBinder internalToken(Class<?> surface) throws Exception {
            if (Build.VERSION.SDK_INT < 34) {
                try {
                    if (Build.VERSION.SDK_INT >= 29) return (IBinder)surface.getMethod("getInternalDisplayToken").invoke(null);
                    return (IBinder)surface.getMethod("getBuiltInDisplay", int.class).invoke(null, 0);
                } catch (NoSuchMethodException missing) { }
            }
            Class<?> controls = surface;
            if (Build.VERSION.SDK_INT >= 34) {
                // Android 14 起物理 token 由 system_server 的 DisplayControl 提供。
                String classpath = System.getenv("SYSTEMSERVERCLASSPATH");
                if (classpath == null || classpath.isEmpty()) throw new Failure("display_control_unavailable");
                Class<?> factory = Class.forName("com.android.internal.os.ClassLoaderFactory");
                Method create = factory.getDeclaredMethod("createClassLoader", String.class, String.class, String.class,
                    ClassLoader.class, int.class, boolean.class, String.class);
                ClassLoader loader = (ClassLoader)create.invoke(null, classpath, null, null,
                    ClassLoader.getSystemClassLoader(), 0, true, null);
                controls = loader.loadClass("com.android.server.display.DisplayControl");
                Method load = Runtime.class.getDeclaredMethod("loadLibrary0", Class.class, String.class);
                load.setAccessible(true);
                load.invoke(Runtime.getRuntime(), controls, "android_servers");
            }
            long[] ids = (long[])controls.getMethod("getPhysicalDisplayIds").invoke(null);
            if (ids == null || ids.length == 0) throw new Failure("physical_display_unavailable");
            Object info = displayInfo.invoke(display, 0);
            String unique = (String)info.getClass().getField("uniqueId").get(info);
            Long selected = null;
            if (unique != null && unique.startsWith("local:")) {
                try {
                    long identifier = Long.parseUnsignedLong(unique.substring(6));
                    for (long candidate : ids) if (candidate == identifier) selected = candidate;
                } catch (NumberFormatException ignored) { }
            }
            if (selected == null && ids.length == 1) selected = ids[0];
            if (selected == null) throw new Failure("ambiguous_physical_display");
            return (IBinder)controls.getMethod("getPhysicalDisplayToken", long.class).invoke(null, selected.longValue());
        }

        @Override public Snapshot snapshot() throws Exception {
            Object info = displayInfo.invoke(display, 0);
            if (info == null) throw new Failure("display_unavailable");
            int state = info.getClass().getField("state").getInt(info);
            boolean active = (Boolean)(interactiveHasDisplay ? interactive.invoke(power, 0) : interactive.invoke(power));
            boolean locked = (Boolean)keyguard.invoke(window);
            return new Snapshot(state, active, locked);
        }

        @Override public void power(int mode) throws Exception { setPower.invoke(null, token, mode); }
        @Override public void userActivity() throws Exception {
            activity(NO_CHANGE_LIGHTS);
        }
        @Override public void refreshLights() throws Exception {
            activity(0);
        }
        private void activity(int flags) throws Exception {
            long when = SystemClock.uptimeMillis();
            if (activityHasDisplay) activity.invoke(power, 0, when, 0, flags);
            else activity.invoke(power, when, 0, flags);
        }
    }

    private static String line(BufferedReader input) throws Exception {
        StringBuilder value = new StringBuilder();
        int character;
        while ((character = input.read()) != -1) {
            if (character == '\n') return value.toString();
            if (value.length() >= 1024) throw new Failure("packet_too_large");
            value.append((char)character);
        }
        return value.length() == 0 ? null : value.toString();
    }

    private static String code(Exception error) { return error instanceof Failure ? ((Failure)error).code : "display_power_unavailable"; }

    private static boolean restore(Controller controller, String reason) {
        for (int attempt = 0; attempt < 3; attempt++) {
            try { controller.finish(reason); return true; }
            catch (Exception failure) { try { Thread.sleep(100); } catch (InterruptedException ignored) { } }
        }
        return false;
    }

    public static void main(String[] args) {
        PrintWriter output = new PrintWriter(System.out, true);
        Controller controller = null;
        int exitCode = 0;
        try {
            if (args.length != 0) throw new Failure("unexpected_arguments");
            controller = new Controller(new AndroidBackend(), new Clock() {
                @Override public long now() { return SystemClock.uptimeMillis(); }
            });
            final Controller watched = controller;
            Runtime.getRuntime().addShutdownHook(new Thread(new Runnable() {
                @Override public void run() { restore(watched, "process_exit"); }
            }, "homeai-display-restore"));
            Thread watchdog = new Thread(new Runnable() {
                @Override public void run() {
                    while (true) {
                        try {
                            Thread.sleep(200);
                            if (watched.tick()) break;
                        } catch (Exception failure) { break; }
                    }
                    boolean restored = restore(watched, "watchdog_exit");
                    try { output.println(watched.reply(0, restored, false, restored ? "" : "restore_failed")
                        .put("ready", false).put("event", restored ? "restored" : "restore_failed").toString()); }
                    catch (Exception ignored) { }
                    System.exit(restored ? 0 : 1);
                }
            }, "homeai-display-watchdog");
            watchdog.setDaemon(true);
            watchdog.start();
            output.println(controller.reply(0, true, false, "").put("ready", true).put("protocol", 1).toString());
            BufferedReader input = new BufferedReader(new InputStreamReader(System.in, StandardCharsets.UTF_8));
            String raw;
            while ((raw = line(input)) != null) {
                JSONObject object = new JSONObject(raw);
                Set<String> keys = new HashSet<String>();
                Iterator<String> names = object.keys();
                while (names.hasNext()) keys.add(names.next());
                Request request = validate(keys, object.opt("id"), object.opt("action"));
                try {
                    boolean applied = controller.command(request);
                    output.println(controller.reply(request.id, true, applied, "").toString());
                } catch (Exception failure) {
                    exitCode = 1;
                    boolean restored = restore(controller, "command_failed");
                    output.println(controller.reply(request.id, false, false, restored ? code(failure) : "restore_failed").toString());
                    break;
                }
            }
        } catch (Exception failure) {
            exitCode = 1;
            output.println("{\"id\":0,\"ok\":false,\"supported\":false,\"applied\":false,\"physical_verified\":false,\"error\":\"" + code(failure) + "\"}");
        } finally {
            if (controller != null) {
                boolean restored = restore(controller, "stdin_closed");
                if (!restored) exitCode = 1;
                try { output.println(controller.reply(0, restored, false, restored ? "" : "restore_failed")
                    .put("ready", false).put("event", restored ? "restored" : "restore_failed").toString()); }
                catch (Exception ignored) { }
            }
        }
        System.exit(exitCode);
    }
}
