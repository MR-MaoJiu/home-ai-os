package dev.homeai.androidinput;

import android.os.IBinder;
import android.os.SystemClock;
import android.view.InputDevice;
import android.view.InputEvent;
import android.view.MotionEvent;
import org.json.JSONArray;
import org.json.JSONObject;
import java.io.BufferedReader;
import java.io.InputStreamReader;
import java.io.PrintWriter;
import java.lang.reflect.Method;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.HashSet;
import java.util.Set;

/** 仅通过已授权 ADB 的标准输入接收触点，不创建网络端口，不执行 Shell。 */
public final class TouchServer {
    private static final Object LOCK = new Object();
    private static Object inputManager;
    private static Method injectMethod;
    private static Object displayManager;
    private static Method displayInfoMethod;
    private static final ArrayList<Point> active = new ArrayList<Point>();
    private static long downTime;
    private static long lastPacket = SystemClock.uptimeMillis();
    private static int width, height, rotation;
    private static int gestureWidth, gestureHeight, gestureRotation;
    private static boolean closing;
    private static int cancellations;
    private static String lastCancelReason = "";

    private static final class Point {
        int id; float x, y;
        Point(int id, float x, float y) { this.id = id; this.x = x; this.y = y; }
    }

    private static void initialize() throws Exception {
        Class<?> input;
        try { input = Class.forName("android.hardware.input.InputManagerGlobal"); }
        catch (ClassNotFoundException missing) { input = Class.forName("android.hardware.input.InputManager"); }
        inputManager = input.getDeclaredMethod("getInstance").invoke(null);
        injectMethod = inputManager.getClass().getMethod("injectInputEvent", InputEvent.class, int.class);
        Class<?> services = Class.forName("android.os.ServiceManager");
        IBinder binder = (IBinder) services.getMethod("getService", String.class).invoke(null, "display");
        displayManager = Class.forName("android.hardware.display.IDisplayManager$Stub")
            .getMethod("asInterface", IBinder.class).invoke(null, binder);
        displayInfoMethod = displayManager.getClass().getMethod("getDisplayInfo", int.class);
        displayInfoMethod.setAccessible(true);
        geometry();
    }

    private static void geometry() throws Exception {
        Object info = displayInfoMethod.invoke(displayManager, 0);
        if (info == null) throw new IllegalStateException("display_unavailable");
        width = info.getClass().getField("logicalWidth").getInt(info);
        height = info.getClass().getField("logicalHeight").getInt(info);
        rotation = info.getClass().getField("rotation").getInt(info);
        if (width < 1 || height < 1 || width > 8192 || height > 8192 || rotation < 0 || rotation > 3)
            throw new IllegalStateException("display_invalid");
    }

    private static Set<Integer> ids(ArrayList<Point> points) {
        Set<Integer> result = new HashSet<Integer>();
        for (Point point : points) result.add(point.id);
        return result;
    }

    private static void inject(int action, ArrayList<Point> points) throws Exception {
        if (points.isEmpty()) return;
        MotionEvent.PointerProperties[] properties = new MotionEvent.PointerProperties[points.size()];
        MotionEvent.PointerCoords[] coords = new MotionEvent.PointerCoords[points.size()];
        for (int i = 0; i < points.size(); i++) {
            Point point = points.get(i);
            properties[i] = new MotionEvent.PointerProperties();
            properties[i].id = point.id; properties[i].toolType = MotionEvent.TOOL_TYPE_FINGER;
            coords[i] = new MotionEvent.PointerCoords();
            coords[i].x = point.x; coords[i].y = point.y; coords[i].size = 1;
            coords[i].pressure = (action & MotionEvent.ACTION_MASK) == MotionEvent.ACTION_CANCEL ||
                (action & MotionEvent.ACTION_MASK) == MotionEvent.ACTION_UP ? 0 : 1;
        }
        MotionEvent event = MotionEvent.obtain(downTime, SystemClock.uptimeMillis(), action, points.size(),
            properties, coords, 0, 0, 1, 1, -1, 0, InputDevice.SOURCE_TOUCHSCREEN, 0);
        try {
            // 等待输入调度器接受，不等待应用整帧渲染；返回 false 不能伪报成功。
            if (!Boolean.TRUE.equals(injectMethod.invoke(inputManager, event, 1)))
                throw new IllegalStateException("injection_rejected");
        } finally { event.recycle(); }
    }

    private static void cancel(String reason) throws Exception {
        if (!active.isEmpty()) {
            inject(MotionEvent.ACTION_CANCEL, active);
            active.clear(); cancellations++; lastCancelReason = reason;
        }
    }

    private static JSONObject reply(boolean ok, long identifier, String error) throws Exception {
        return new JSONObject().put("id", identifier).put("ok", ok).put("error", error)
            .put("width", width).put("height", height).put("rotation", rotation)
            .put("active", active.size()).put("cancellations", cancellations).put("cancel_reason", lastCancelReason);
    }

    private static JSONObject handle(JSONObject packet) throws Exception {
        long identifier = packet.optLong("id", -1);
        geometry();
        if (!active.isEmpty() && (width != gestureWidth || height != gestureHeight || rotation != gestureRotation)) {
            cancel("display_changed");
            return reply(false, identifier, "display_changed");
        }
        String phase = packet.getString("phase");
        lastPacket = SystemClock.uptimeMillis();
        if ("cancel".equals(phase)) { cancel("requested"); return reply(true, identifier, ""); }
        if ("keepalive".equals(phase)) return reply(true, identifier, "");
        JSONArray values = packet.getJSONArray("points");
        if (values.length() < 1 || values.length() > 5) throw new IllegalArgumentException("invalid_points");
        ArrayList<Point> points = new ArrayList<Point>();
        for (int i = 0; i < values.length(); i++) {
            JSONObject value = values.getJSONObject(i);
            int id = value.getInt("id");
            double x = value.getDouble("x_px"), y = value.getDouble("y_px");
            if (id < 0 || id > 9 || !Double.isFinite(x) || !Double.isFinite(y) || x < 0 || y < 0 || x >= width || y >= height)
                throw new IllegalArgumentException("invalid_point");
            points.add(new Point(id, (float)x, (float)y));
        }
        Set<Integer> before = ids(active), after = ids(points);
        if (after.size() != points.size()) throw new IllegalArgumentException("duplicate_pointer");
        int changed = packet.optInt("changed_id", -1), index = -1;
        for (int i = 0; i < points.size(); i++) if (points.get(i).id == changed) index = i;
        int action;
        if ("down".equals(phase)) {
            if (!active.isEmpty() || points.size() != 1 || index != 0) throw new IllegalArgumentException("invalid_down");
            downTime = SystemClock.uptimeMillis();
            gestureWidth = width; gestureHeight = height; gestureRotation = rotation;
            action = MotionEvent.ACTION_DOWN;
        } else if ("pointer_down".equals(phase)) {
            Set<Integer> added = new HashSet<Integer>(after); added.removeAll(before);
            if (active.isEmpty() || points.size() != active.size()+1 || !after.containsAll(before) || added.size()!=1 || !added.contains(changed))
                throw new IllegalArgumentException("invalid_pointer_down");
            action = MotionEvent.ACTION_POINTER_DOWN | (index << MotionEvent.ACTION_POINTER_INDEX_SHIFT);
        } else if ("move".equals(phase)) {
            if (active.isEmpty() || !before.equals(after)) throw new IllegalArgumentException("invalid_move");
            action = MotionEvent.ACTION_MOVE;
        } else if ("pointer_up".equals(phase)) {
            if (active.size() < 2 || !before.equals(after) || index < 0) throw new IllegalArgumentException("invalid_pointer_up");
            action = MotionEvent.ACTION_POINTER_UP | (index << MotionEvent.ACTION_POINTER_INDEX_SHIFT);
        } else if ("up".equals(phase)) {
            if (active.size() != 1 || !before.equals(after) || index != 0) throw new IllegalArgumentException("invalid_up");
            action = MotionEvent.ACTION_UP;
        } else throw new IllegalArgumentException("invalid_phase");
        inject(action, points);
        active.clear(); active.addAll(points);
        if ("pointer_up".equals(phase)) active.remove(index);
        else if ("up".equals(phase)) active.clear();
        return reply(true, identifier, "");
    }

    public static void main(String[] args) {
        final PrintWriter output = new PrintWriter(System.out, true);
        try {
            initialize();
            output.println(reply(true, 0, "").put("ready", true).toString());
            Thread watchdog = new Thread(new Runnable() {
                @Override public void run() {
                    while (true) {
                        try { Thread.sleep(100); } catch (InterruptedException ignored) { return; }
                        synchronized (LOCK) {
                            if (closing) return;
                            if (!active.isEmpty()) {
                                try {
                                    geometry();
                                    if (SystemClock.uptimeMillis() - lastPacket > 1500) cancel("timeout");
                                    else if (width != gestureWidth || height != gestureHeight || rotation != gestureRotation) cancel("display_changed");
                                } catch (Exception failure) {
                                    try { cancel("device_error"); } catch (Exception ignored) { }
                                }
                            }
                        }
                    }
                }
            }, "homeai-touch-watchdog");
            watchdog.setDaemon(true); watchdog.start();
            BufferedReader input = new BufferedReader(new InputStreamReader(System.in, StandardCharsets.UTF_8));
            String line;
            while ((line = input.readLine()) != null) {
                synchronized (LOCK) {
                    JSONObject packet = null;
                    try {
                        if (line.length() > 8192) throw new IllegalArgumentException("packet_too_large");
                        packet = new JSONObject(line);
                        output.println(handle(packet).toString());
                    } catch (Exception failure) {
                        try { cancel("invalid_or_failed_input"); } catch (Exception ignored) { }
                        String reason = failure instanceof IllegalArgumentException ? "invalid_packet" : "input_unavailable";
                        output.println(reply(false, packet == null ? -1 : packet.optLong("id", -1), reason).toString());
                    }
                }
            }
        } catch (Exception failure) {
            output.println("{\"ok\":false,\"error\":\"bridge_unavailable\"}");
        } finally {
            synchronized (LOCK) {
                try { cancel("closed"); } catch (Exception ignored) { }
                closing = true;
            }
        }
    }
}
