package io.github.xiaofan6ya.workbuddy2api;

import android.app.*;
import android.content.Intent;
import android.content.pm.ServiceInfo;
import android.net.ConnectivityManager;
import android.net.NetworkCapabilities;
import android.os.*;
import com.chaquo.python.Python;
import com.chaquo.python.PyObject;
import com.chaquo.python.android.AndroidPlatform;
import org.json.JSONObject;
import java.util.concurrent.atomic.AtomicInteger;

public class LocalApiService extends Service {
    static volatile String origin, token, error;
    static volatile boolean starting;
    static volatile boolean registered;
    static volatile long lastHealthy;
    static volatile long startedAt;
    static volatile String startupStage = "等待启动本地服务";
    static volatile int revision;
    private static final String CHANNEL = "local-api";
    private static final Object RUNTIME_LOCK = new Object();
    private static final AtomicInteger generation = new AtomicInteger();
    private static final java.util.ArrayDeque<String> startupEvents = new java.util.ArrayDeque<>();
    static String safeDiagnostic(String text) {
        if (text == null) return "";
        return text.replaceAll("(?i)(Bearer\\s+|sk-)[A-Za-z0-9._~+/-]+", "$1[REDACTED]")
            .replaceAll("eyJ[A-Za-z0-9_-]+\\.[A-Za-z0-9_-]+\\.[A-Za-z0-9_-]+", "[JWT REDACTED]")
            .replaceAll("(?i)(token|password|secret|authorization|api[_-]?key)([\\\"'\\s:=]+)[^\\s,}]+", "$1$2[REDACTED]")
            .replaceAll("(https?://[^\\s?]+)\\?[^\\s]+", "$1?[REDACTED]");
    }
    static synchronized void noteStartup(String message) {
        if (startupEvents.size() >= 60) startupEvents.removeFirst();
        long seconds = startedAt == 0 ? 0 : Math.max(0, SystemClock.elapsedRealtime() - startedAt) / 1000;
        startupEvents.addLast("+" + seconds + "s " + safeDiagnostic(message));
    }
    static synchronized String startupHistory() { return android.text.TextUtils.join("\n", startupEvents); }
    private int instanceGeneration;
    private Thread worker;
    private PowerManager.WakeLock wakeLock;
    private static volatile boolean wakeLockActive;
    static boolean wakeLockHeld() { return wakeLockActive; }
    private volatile boolean stopping;
    private final Handler monitor = new Handler(Looper.getMainLooper());
    private boolean probing;
    private int failedChecks;
    private int startFailures;
    private final Runnable healthCheck = new Runnable() {
        @Override public void run() {
            if (stopping) return;
            if (!starting && !probing && origin != null) {
                probing = true;
                new Thread(() -> {
                    boolean healthy = false, alive = true;
                    int active = 0;
                    try {
                        JSONObject state = new JSONObject(Python.getInstance().getModule("android_runtime").callAttr("health_status").toString());
                        healthy = state.optBoolean("responsive");
                        alive = state.optBoolean("alive", true);
                        active = state.optInt("active_requests");
                    }
                    catch (Exception ignored) { }
                    final boolean result = healthy;
                    final boolean runtimeAlive = alive;
                    final int requests = active;
                    monitor.post(() -> {
                        probing = false;
                        if (stopping || starting) return;
                        if (result) { lastHealthy = System.currentTimeMillis(); failedChecks = 0; }
                        else if (!runtimeAlive) {
                            failedChecks = 0;
                            noteStartup("服务线程已退出，开始恢复");
                            launch(true);
                        } else if (++failedChecks == 2) {
                            // Android background scheduling can delay both Python and
                            // localhost HTTP. Restarting here used to cut active SSE.
                            noteStartup("监听检查响应延迟，服务线程仍存活，保留当前连接（活动请求 " + requests + "）");
                        }
                    });
                }, "workbuddy-service-health").start();
            }
            monitor.postDelayed(this, 15000);
        }
    };

    @Override public void onCreate() {
        super.onCreate();
        instanceGeneration = generation.incrementAndGet();
        origin = null; token = null; error = null; lastHealthy = 0; starting = true;
        if (Build.VERSION.SDK_INT >= 26) {
            NotificationChannel channel = new NotificationChannel(CHANNEL, "本地 API 服务", NotificationManager.IMPORTANCE_LOW);
            getSystemService(NotificationManager.class).createNotificationChannel(channel);
        }
        Notification notification = notification("正在启动本地 API 服务…");
        if (Build.VERSION.SDK_INT >= 34) startForeground(1, notification, ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE);
        else startForeground(1, notification);
        registered = true;
        PowerManager power = (PowerManager) getSystemService(POWER_SERVICE);
        wakeLock = power.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, getPackageName() + ":local-api");
        wakeLock.acquire();
        wakeLockActive = true;
        monitor.postDelayed(healthCheck, 15000);
    }

    private Notification notification(String text) {
        int flags = PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE;
        PendingIntent open = PendingIntent.getActivity(this, 0, new Intent(this, MainActivity.class), flags);
        PendingIntent stop = PendingIntent.getService(this, 1, new Intent(this, LocalApiService.class).setAction("stop"), flags);
        Notification.Builder builder = Build.VERSION.SDK_INT >= 26 ? new Notification.Builder(this, CHANNEL) : new Notification.Builder(this);
        return builder.setSmallIcon(R.drawable.notification_icon).setContentTitle("wb2api")
            .setContentText(text).setContentIntent(open).setOngoing(true)
            .addAction(new Notification.Action.Builder(null, "停止服务", stop).build()).build();
    }

    @Override public int onStartCommand(Intent intent, int flags, int startId) {
        if (intent != null && "stop".equals(intent.getAction())) { stopSelf(); return START_NOT_STICKY; }
        launch(intent != null && "restart".equals(intent.getAction()));
        return START_STICKY;
    }

    private void launch(boolean restart) {
        if (stopping || (worker != null && worker.isAlive())) return;
        error = null;
        starting = true;
        startedAt = SystemClock.elapsedRealtime();
        startupStage = "正在初始化 Python 运行环境";
        noteStartup(startupStage);
        getSystemService(NotificationManager.class).notify(1, notification(restart ? "正在恢复本地 API 服务…" : "正在检查本地 API 服务…"));
        worker = new Thread(() -> {
            try {
                synchronized (RUNTIME_LOCK) {
                    if (!Python.isStarted()) Python.start(new AndroidPlatform(this));
                    startupStage = "正在加载本地服务模块";
                    noteStartup(startupStage);
                    PyObject runtime = Python.getInstance().getModule("android_runtime");
                    if (stopping) return;
                    if (restart) { origin = null; token = null; runtime.callAttr("stop"); }
                    JSONObject state = new JSONObject(runtime.callAttr("start", getFilesDir().getAbsolutePath(), this).toString());
                    if (stopping || generation.get() != instanceGeneration) return;
                    token = state.getString("token");
                    origin = state.getString("origin");
                    if (restart || revision == 0) revision++;
                    lastHealthy = System.currentTimeMillis();
                    startupStage = "本地服务已启动";
                    noteStartup(startupStage);
                    startFailures = 0;
                    getSystemService(NotificationManager.class).notify(1, notification(origin + "/v1 · 服务运行中"));
                }
            } catch (Exception | LinkageError e) {
                if (!stopping && generation.get() == instanceGeneration) {
                    error = safeDiagnostic("启动失败：" + e.getClass().getSimpleName() + ": " + e.getMessage());
                    noteStartup(error);
                    origin = null; token = null;
                    final long delay = Math.min(60000L, 5000L << Math.min(startFailures++, 3));
                    getSystemService(NotificationManager.class).notify(1, notification("服务暂不可用，正在等待自动恢复"));
                    monitor.postDelayed(() -> { if (!stopping) launch(true); }, delay);
                }
            } finally {
                if (generation.get() == instanceGeneration) starting = false;
            }
        }, "workbuddy-python-start");
        worker.start();
    }

    public void startupProgress(String stage) {
        if (!stopping && generation.get() == instanceGeneration) { startupStage = stage; noteStartup(stage); }
    }

    public void prepareApiThread() {
        // Do not inherit a Java worker's background priority for the API loop.
        android.os.Process.setThreadPriority(android.os.Process.THREAD_PRIORITY_DEFAULT);
    }

    public String runtimeDiagnostics() {
        JSONObject data = new JSONObject();
        try {
            PowerManager power = (PowerManager)getSystemService(POWER_SERVICE);
            ActivityManager.RunningAppProcessInfo process = new ActivityManager.RunningAppProcessInfo();
            ActivityManager.getMyMemoryState(process);
            data.put("android_sdk", Build.VERSION.SDK_INT);
            data.put("manufacturer", Build.MANUFACTURER);
            data.put("device_model", Build.MODEL);
            data.put("process_importance", process.importance);
            data.put("interactive", power.isInteractive());
            data.put("device_idle", power.isDeviceIdleMode());
            data.put("power_save", power.isPowerSaveMode());
            data.put("battery_unrestricted", power.isIgnoringBatteryOptimizations(getPackageName()));
            data.put("wake_lock", wakeLock != null && wakeLock.isHeld());
            data.put("service_revision", revision);
            ConnectivityManager connectivity = (ConnectivityManager)getSystemService(CONNECTIVITY_SERVICE);
            NetworkCapabilities caps = connectivity.getNetworkCapabilities(connectivity.getActiveNetwork());
            data.put("network_available", caps != null);
            data.put("network_validated", caps != null && caps.hasCapability(NetworkCapabilities.NET_CAPABILITY_VALIDATED));
            data.put("wifi", caps != null && caps.hasTransport(NetworkCapabilities.TRANSPORT_WIFI));
            data.put("cellular", caps != null && caps.hasTransport(NetworkCapabilities.TRANSPORT_CELLULAR));
            if (Build.VERSION.SDK_INT >= 28) data.put("background_restricted", getSystemService(ActivityManager.class).isBackgroundRestricted());
        } catch (Exception ignored) { }
        return data.toString();
    }

    @Override public void onDestroy() {
        stopping = true;
        monitor.removeCallbacksAndMessages(null);
        if (generation.get() == instanceGeneration) {
            registered = false; starting = false; lastHealthy = 0;
            wakeLockActive = false;
            origin = null; token = null;
        }
        new Thread(() -> {
            if (worker != null) try { worker.join(95000); } catch (InterruptedException ignored) { }
            synchronized (RUNTIME_LOCK) {
                // A delayed cleanup must not stop a service the user just restarted.
                if (generation.get() == instanceGeneration && Python.isStarted())
                    try { Python.getInstance().getModule("android_runtime").callAttr("stop"); } catch (Exception ignored) { }
            }
        }, "workbuddy-python-stop").start();
        if (wakeLock != null && wakeLock.isHeld()) wakeLock.release();
        stopForeground(true);
        super.onDestroy();
    }

    @Override public IBinder onBind(Intent intent) { return null; }
}
