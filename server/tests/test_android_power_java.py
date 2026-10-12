"""在普通 JVM 验证电源租约状态机，并编译为 Android dex；不连接或控制设备。"""
import os
from pathlib import Path
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[2]
HARNESS = r'''
package dev.homeai.androidinput;
import java.util.*;
public final class PowerHarness {
    static class Time implements PowerServer.Clock {
        long value;
        public long now() { return value; }
    }
    static class Device implements PowerServer.Backend {
        PowerServer.Snapshot state = new PowerServer.Snapshot(2, true, false);
        List<Integer> powers = new ArrayList<Integer>();
        int activities, lightRefreshes;
        boolean failOff, failRestore, failRefresh, lockAfterRestore;
        public PowerServer.Snapshot snapshot() { return state; }
        public void power(int mode) throws Exception {
            powers.add(mode);
            if (mode == 0 && failOff) { failOff=false; throw new Exception("公开失败样例"); }
            if (mode == 2 && failRestore) { failRestore=false; throw new Exception("公开恢复失败样例"); }
            if (mode == 2 && lockAfterRestore) state=new PowerServer.Snapshot(2,true,true);
        }
        public void userActivity() { activities++; }
        public void refreshLights() throws Exception {
            lightRefreshes++;
            if (failRefresh) throw new Exception("公开光照刷新失败样例");
        }
    }
    static PowerServer.Request request(long id, String action) {
        return new PowerServer.Request(id, action);
    }
    static void require(boolean value) { if (!value) throw new AssertionError(); }
    static void expectFailure(PowerServer.Controller c, PowerServer.Request r, String code) throws Exception {
        try { c.command(r); throw new AssertionError(); }
        catch (PowerServer.Failure failure) { require(failure.code.equals(code)); }
    }
    public static void main(String[] args) throws Exception {
        Time time = new Time(); Device device = new Device();
        PowerServer.Controller c = new PowerServer.Controller(device, time);
        String scenario = args[0];
        if (scenario.equals("off_on")) {
            require(c.command(request(1,"off")));
            require(!c.command(request(2,"off")));
            require(c.command(request(3,"on")));
            c.finish("stdin_closed");
            require(device.powers.equals(Arrays.asList(0,2)) && device.activities==0 && device.lightRefreshes==1);
        } else if (scenario.equals("refresh_only")) {
            require(c.command(request(1,"on")));
            c.finish("stdin_closed");
            require(device.powers.isEmpty() && device.activities==0 && device.lightRefreshes==1);
        } else if (scenario.equals("refresh_locked")) {
            device.state=new PowerServer.Snapshot(2,true,true);
            require(!c.command(request(1,"on")));
            c.finish("stdin_closed");
            require(device.powers.isEmpty() && device.activities==0 && device.lightRefreshes==0);
        } else if (scenario.equals("refresh_sleeping")) {
            device.state=new PowerServer.Snapshot(1,false,false);
            require(!c.command(request(1,"on")));
            c.finish("stdin_closed");
            require(device.powers.isEmpty() && device.activities==0 && device.lightRefreshes==0);
        } else if (scenario.equals("lock_during_restore")) {
            c.command(request(1,"off")); device.lockAfterRestore=true;
            require(c.command(request(2,"on")));
            c.finish("stdin_closed");
            require(device.powers.equals(Arrays.asList(0,2)) && device.lightRefreshes==0 && device.state.locked);
        } else if (scenario.equals("failed_refresh_once")) {
            c.command(request(1,"off")); device.failRefresh=true;
            try { c.command(request(2,"on")); throw new AssertionError(); }
            catch (Exception expected) { }
            c.finish("command_failed");
            require(device.powers.equals(Arrays.asList(0,2)) && device.lightRefreshes==1);
        } else if (scenario.equals("heartbeat")) {
            c.command(request(1,"off")); c.tick();
            require(device.activities==1 && PowerServer.NO_CHANGE_LIGHTS==1);
            for (int i=1;i<=8;i++) {
                time.value=i*1000;
                c.command(request(i+1,"keepalive"));
                require(!c.tick());
            }
            require(device.activities==9);
            require(device.lightRefreshes==0);
            c.finish("stdin_closed");
            require(device.powers.equals(Arrays.asList(0,2)) && device.lightRefreshes==1);
        } else if (scenario.equals("timeout")) {
            c.command(request(1,"off")); time.value=PowerServer.LEASE_MS;
            require(c.tick());
            expectFailure(c,request(2,"off"),"lease_ended");
            require(device.powers.equals(Arrays.asList(0,2)));
        } else if (scenario.equals("late_heartbeat")) {
            c.command(request(1,"off")); time.value=PowerServer.LEASE_MS;
            expectFailure(c,request(2,"keepalive"),"lease_expired");
            require(device.powers.equals(Arrays.asList(0,2)));
        } else if (scenario.equals("locked")) {
            device.state=new PowerServer.Snapshot(2,true,true);
            expectFailure(c,request(1,"off"),"device_locked");
            c.finish("stdin_closed"); require(device.powers.isEmpty());
        } else if (scenario.equals("sleeping")) {
            device.state=new PowerServer.Snapshot(1,false,true);
            require(!c.command(request(1,"on")));
            expectFailure(c,request(2,"off"),"device_locked");
            c.finish("stdin_closed"); require(device.powers.isEmpty());
        } else if (scenario.equals("sleep_after_off")) {
            c.command(request(1,"off"));
            device.state=new PowerServer.Snapshot(1,false,true);
            require(c.tick()); c.finish("stdin_closed");
            require(device.powers.equals(Arrays.asList(0)) && device.activities==0 && device.lightRefreshes==0);
        } else if (scenario.equals("lock_after_off")) {
            c.command(request(1,"off"));
            device.state=new PowerServer.Snapshot(2,true,true);
            require(c.tick()); require(device.state.locked);
            require(device.powers.equals(Arrays.asList(0,2)) && device.activities==0 && device.lightRefreshes==0);
        } else if (scenario.equals("failed_off")) {
            device.failOff=true;
            try { c.command(request(1,"off")); throw new AssertionError(); }
            catch (Exception expected) { }
            c.finish("command_failed");
            require(device.powers.equals(Arrays.asList(0,2)));
        } else if (scenario.equals("failed_restore")) {
            c.command(request(1,"off")); device.failRestore=true;
            try { c.finish("stdin_closed"); throw new AssertionError(); }
            catch (Exception expected) { }
            c.finish("stdin_closed");
            require(device.powers.equals(Arrays.asList(0,2,2)));
        } else if (scenario.equals("stale")) {
            c.command(request(1,"off"));
            expectFailure(c,request(1,"on"),"stale_request");
            c.finish("stdin_closed"); require(device.powers.equals(Arrays.asList(0,2)));
        } else if (scenario.equals("contract")) {
            Set<String> keys=new HashSet<String>(Arrays.asList("id","action"));
            require(PowerServer.validate(keys,1,"status").id==1);
            for (Object badId : Arrays.<Object>asList(0,-1,"1",1.0,true)) {
                try { PowerServer.validate(keys,badId,"off"); throw new AssertionError(); }
                catch (PowerServer.Failure expected) { }
            }
            for (String badAction : Arrays.asList("shell","unlock","wake","OFF")) {
                try { PowerServer.validate(keys,1,badAction); throw new AssertionError(); }
                catch (PowerServer.Failure expected) { }
            }
            keys.add("command");
            try { PowerServer.validate(keys,1,"off"); throw new AssertionError(); }
            catch (PowerServer.Failure expected) { }
        } else throw new AssertionError();
        System.out.println("POWER_STATE_MACHINE_OK");
    }
}
'''


@pytest.fixture(scope='module')
def compiled_power(tmp_path_factory):
    sdk = Path(os.environ.get('ANDROID_SDK_ROOT', Path.home() / 'Library/Android/sdk'))
    java = Path(os.environ.get('JAVA_HOME', '/Library/Java/JavaVirtualMachines/openjdk-17.jdk/Contents/Home'))
    platforms = sorted(sdk.glob('platforms/android-*/android.jar'), reverse=True)
    if not platforms or not (java / 'bin/javac').exists():
        pytest.skip('Java 电源状态机验收需要本机 JDK 与 Android SDK，不会自动下载或连接设备')
    directory = tmp_path_factory.mktemp('android-power-java')
    harness = directory / 'PowerHarness.java'
    harness.write_text(HARNESS)
    sources = ROOT / 'android-input/src/dev/homeai/androidinput'
    result = subprocess.run([str(java / 'bin/javac'), '-source', '8', '-target', '8',
        '-classpath', str(platforms[0]), '-d', str(directory), str(sources / 'PowerServer.java'), str(harness)],
        capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    return java, platforms[0], directory, sdk


@pytest.mark.parametrize('scenario', ['off_on', 'heartbeat', 'timeout', 'late_heartbeat', 'locked',
    'sleeping', 'sleep_after_off', 'lock_after_off', 'failed_off', 'failed_restore', 'stale', 'contract',
    'refresh_only', 'refresh_locked', 'refresh_sleeping', 'lock_during_restore', 'failed_refresh_once'])
def test_power_state_machine_on_host_jvm(compiled_power, scenario):
    java, android, directory, _ = compiled_power
    result = subprocess.run([str(java / 'bin/java'), '-classpath', os.pathsep.join((str(directory), str(android))),
        'dev.homeai.androidinput.PowerHarness', scenario], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == 'POWER_STATE_MACHINE_OK'


def test_power_server_compiles_to_android_dex(compiled_power):
    java, android, directory, sdk = compiled_power
    d8s = sorted(sdk.glob('build-tools/*/d8'), reverse=True)
    if not d8s:
        pytest.skip('未安装 d8，保留普通 JVM 状态机验收结果')
    output = directory / 'dex'
    output.mkdir()
    classes = sorted(directory.glob('dev/homeai/androidinput/PowerServer*.class'))
    environment = dict(os.environ, JAVA_HOME=str(java))
    result = subprocess.run([str(d8s[0]), '--lib', str(android), '--min-api', '26', '--output', str(output),
        *(str(path) for path in classes)], capture_output=True, text=True, env=environment, timeout=30)
    assert result.returncode == 0, result.stderr
    assert (output / 'classes.dex').read_bytes().startswith(b'dex\n')
