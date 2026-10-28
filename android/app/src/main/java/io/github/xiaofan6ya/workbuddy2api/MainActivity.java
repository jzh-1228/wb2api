package io.github.xiaofan6ya.workbuddy2api;

import android.Manifest;
import android.app.Activity;
import android.content.*;
import android.content.pm.PackageManager;
import android.graphics.Color;
import android.graphics.Typeface;
import android.graphics.drawable.GradientDrawable;
import android.net.Uri;
import android.os.*;
import android.view.*;
import android.webkit.*;
import android.widget.*;
import org.json.JSONObject;
import com.chaquo.python.Python;
import java.io.OutputStream;
import java.nio.charset.StandardCharsets;

public class MainActivity extends Activity {
    private static boolean startupTransitionShown;
    private final Handler handler = new Handler(Looper.getMainLooper());
    private WebView web;

    private void applySystemTheme(boolean dark) {
        int background = dark ? Color.rgb(30, 35, 44) : Color.WHITE;
        getWindow().getDecorView().setSystemUiVisibility(View.SYSTEM_UI_FLAG_LAYOUT_STABLE |
            View.SYSTEM_UI_FLAG_LAYOUT_FULLSCREEN | View.SYSTEM_UI_FLAG_LAYOUT_HIDE_NAVIGATION |
            (dark ? 0 : View.SYSTEM_UI_FLAG_LIGHT_STATUS_BAR) |
            (!dark && Build.VERSION.SDK_INT >= 26 ? View.SYSTEM_UI_FLAG_LIGHT_NAVIGATION_BAR : 0));
        getWindow().getDecorView().setBackgroundColor(background);
        if (web != null) {
            web.setBackgroundColor(background);
            ((View) web.getParent()).setBackgroundColor(background);
        }
    }
    private TextView status;
    private FrameLayout boot;
    private TextView bootTitle;
    private ProgressBar progress;
    private Button details;
    private ValueCallback<Uri[]> upload;
    private String loadedOrigin;
    private int loadedRevision;
    private boolean destroyed;
    private long pageStartedAt;
    private int pageGeneration;
    private boolean workbenchReady, checkingPage;
    private String pageError;
    private volatile boolean bridgeTrusted;
    private Button restart;
    private volatile String exportRequest;
    private byte[] exportBytes;
    private volatile boolean exportWriting;
    private byte[] startupExport;
    private final Runnable waitForServer = new Runnable() {
        @Override public void run() {
            if (destroyed) return;
            String origin = LocalApiService.origin;
            if (origin != null && !LocalApiService.starting &&
                    (!origin.equals(loadedOrigin) || loadedRevision != LocalApiService.revision)) {
                loadedOrigin = origin;
                loadedRevision = LocalApiService.revision;
                loadWorkbench();
            } else if (origin != null && !workbenchReady && !LocalApiService.starting) {
                checkWorkbench();
                if (pageStartedAt > 0 && SystemClock.elapsedRealtime() - pageStartedAt > 30000) {
                    setStartupText(bootTitle, "工作台加载超时");
                    setStartupText(status, "本地服务已启动，页面尚未完成初始化。可重新加载，或查看错误详情。");
                    progress.setVisibility(View.GONE); setStartupText(restart, "重新加载工作台");
                    restart.setVisibility(View.VISIBLE); details.setVisibility(View.VISIBLE);
                }
            } else if (origin == null) {
                showStartupPage();
                boolean failed = LocalApiService.error != null;
                setStartupText(bootTitle, failed ? "服务启动失败" : LocalApiService.starting ? "正在打开工作台" : "本地服务已停止");
                long elapsed = LocalApiService.starting ? Math.max(0, SystemClock.elapsedRealtime() - LocalApiService.startedAt) / 1000 : 0;
                boolean slow = elapsed >= 120;
                setStartupText(status, failed ? "请重试启动。若仍无法打开，可查看错误详情。" : LocalApiService.starting ? LocalApiService.startupStage + "\n已等待 " + elapsed + " 秒" + (slow ? "\n启动长时间未完成。请关闭应用后重新打开，无需清除数据。" : "") : "点击下方按钮，继续使用手机本地 API。");
                progress.setVisibility(LocalApiService.starting && !slow ? View.VISIBLE : View.GONE);
                setStartupText(restart, "启动服务");
                restart.setVisibility(LocalApiService.starting ? View.GONE : View.VISIBLE);
                details.setVisibility(failed || elapsed >= 30 ? View.VISIBLE : View.GONE);
                if (loadedOrigin != null) { loadedOrigin = null; web.loadUrl("about:blank"); }
            }
            handler.postDelayed(this, 500);
        }
    };

    private static void setStartupText(TextView view, String text) {
        if (!android.text.TextUtils.equals(view.getText(), text)) view.setText(text);
    }

    private void cancelStartupTransition() {
        boot.animate().withEndAction(null).cancel();
        web.animate().cancel();
        boot.setAlpha(1f);
        web.setAlpha(1f);
    }

    private void showStartupPage() {
        cancelStartupTransition();
        boot.setVisibility(View.VISIBLE);
    }

    private void enterWorkbench() {
        cancelStartupTransition();
        boolean animate = !startupTransitionShown && (Build.VERSION.SDK_INT >= 26
            ? android.animation.ValueAnimator.areAnimatorsEnabled()
            : android.provider.Settings.Global.getFloat(getContentResolver(),
                android.provider.Settings.Global.ANIMATOR_DURATION_SCALE, 1f) > 0f);
        // One entrance per app process; returning from the background or reloading stays immediate.
        startupTransitionShown = true;
        if (!animate) {
            boot.setVisibility(View.GONE);
            return;
        }
        final int expected = pageGeneration;
        web.setAlpha(0f);
        web.animate().alpha(1f).setDuration(320)
            .setInterpolator(new android.view.animation.AccelerateDecelerateInterpolator()).start();
        boot.animate().alpha(0f).setDuration(320)
            .setInterpolator(new android.view.animation.AccelerateDecelerateInterpolator())
            .withEndAction(() -> {
                if (!destroyed && expected == pageGeneration && workbenchReady) {
                    boot.setVisibility(View.GONE);
                    boot.setAlpha(1f);
                }
            }).start();
    }

    private void loadWorkbench() {
        pageGeneration++; workbenchReady = false; checkingPage = false; pageError = null;
        pageStartedAt = SystemClock.elapsedRealtime();
        setStartupText(bootTitle, "正在加载工作台"); setStartupText(status, "本地服务已启动，正在初始化页面…");
        showStartupPage(); progress.setVisibility(View.VISIBLE);
        restart.setVisibility(View.GONE); details.setVisibility(View.GONE);
        web.loadUrl(loadedOrigin + "/admin?desktop=1&android=1");
    }

    private void checkWorkbench() {
        if (checkingPage || workbenchReady || !isAdmin(web.getUrl()) || LocalApiService.token == null) return;
        checkingPage = true;
        final int expected = pageGeneration;
        String script = "(function(){try{if(!window.WBMobile || typeof window.WBMobile.initialize !== 'function')return 'waiting';" +
            "WBMobile.initialize(" + JSONObject.quote(LocalApiService.token) + ");" +
            "var app=document.getElementById('app');return app&&!app.classList.contains('hidden')?'ready':'waiting';" +
            "}catch(e){return 'error:'+String(e.name)+': '+String(e.message);}})()";
        web.evaluateJavascript(script, value -> {
            if (destroyed || expected != pageGeneration) return;
            checkingPage = false;
            try {
                String result = String.valueOf(new org.json.JSONTokener(value).nextValue());
                if ("ready".equals(result)) { workbenchReady = true; LocalApiService.noteStartup("工作台初始化完成"); enterWorkbench(); }
                else if (result.startsWith("error:")) pageError = result.substring(6);
            } catch (Exception ignored) { pageError = "无法读取页面初始化结果"; }
        });
    }

    private String startupDetails() {
        android.content.pm.PackageInfo info = Build.VERSION.SDK_INT >= 26 ? WebView.getCurrentWebViewPackage() : null;
        return "wb2api 启动诊断\n时间：" + new java.util.Date() + "\n版本：" + BuildConfig.VERSION_NAME + "\n阶段：" + LocalApiService.startupStage + "\n" +
            "服务启动：" + (LocalApiService.starting ? "进行中" : LocalApiService.origin == null ? "未就绪" : "已就绪") + "\n页面初始化：" + (workbenchReady ? "已完成" : "未完成") + "\n" +
            "服务等待：" + (LocalApiService.startedAt == 0 ? 0 : Math.max(0, SystemClock.elapsedRealtime() - LocalApiService.startedAt) / 1000) + " 秒\n" +
            "系统：Android " + Build.VERSION.RELEASE + "\nWebView：" + (info == null ? "系统内置" : info.packageName + " " + info.versionName) + "\n" +
            (LocalApiService.error == null ? "" : LocalApiService.error + "\n") +
            (pageError == null ? "" : "页面错误：" + LocalApiService.safeDiagnostic(pageError) + "\n") +
            "\n启动阶段记录：\n" + LocalApiService.startupHistory() + "\n\n" +
            "若服务已启动但页面无法初始化，请更新 Android System WebView / Chrome 后重试。无需清除账号数据。";
    }

    private void showStartupDiagnostic() {
        final String report = LocalApiService.safeDiagnostic(startupDetails());
        TextView content = new TextView(this);
        content.setText(report); content.setTextSize(13); content.setTextIsSelectable(true);
        content.setPadding(dp(20), dp(12), dp(20), dp(12));
        ScrollView scroll = new ScrollView(this); scroll.addView(content);
        new android.app.AlertDialog.Builder(this).setTitle("启动诊断").setView(scroll)
            .setPositiveButton("复制诊断", (dialog, which) -> {
                ((ClipboardManager)getSystemService(CLIPBOARD_SERVICE)).setPrimaryClip(ClipData.newPlainText("启动诊断", report));
                Toast.makeText(this, "启动诊断已复制", Toast.LENGTH_SHORT).show();
            }).setNeutralButton("导出文件", (dialog, which) -> {
                startupExport = report.getBytes(StandardCharsets.UTF_8);
                try { startActivityForResult(new Intent(Intent.ACTION_CREATE_DOCUMENT).addCategory(Intent.CATEGORY_OPENABLE)
                    .setType("text/plain").putExtra(Intent.EXTRA_TITLE, "wb2api-startup-diagnostic.txt"), 13); }
                catch (Exception e) { startupExport = null; Toast.makeText(this, "无法打开文件保存器，请复制诊断", Toast.LENGTH_LONG).show(); }
            }).setNegativeButton("关闭", null).show();
    }

    @Override public void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        getWindow().setStatusBarColor(Color.TRANSPARENT);
        getWindow().setNavigationBarColor(Color.TRANSPARENT);
        if (Build.VERSION.SDK_INT >= 29) {
            getWindow().setStatusBarContrastEnforced(false);
            getWindow().setNavigationBarContrastEnforced(false);
        }
        if (Build.VERSION.SDK_INT >= 30) getWindow().setDecorFitsSystemWindows(false);
        FrameLayout layout = new FrameLayout(this);
        layout.setOnApplyWindowInsetsListener((view, insets) -> {
            int top, left, right, bottom;
            if (Build.VERSION.SDK_INT >= 30) {
                android.graphics.Insets bars = insets.getInsets(WindowInsets.Type.systemBars() | WindowInsets.Type.displayCutout());
                android.graphics.Insets keyboard = insets.getInsets(WindowInsets.Type.ime());
                top = bars.top; left = bars.left; right = bars.right; bottom = Math.max(bars.bottom, keyboard.bottom);
            } else {
                top = insets.getSystemWindowInsetTop(); left = insets.getSystemWindowInsetLeft();
                right = insets.getSystemWindowInsetRight(); bottom = insets.getSystemWindowInsetBottom();
            }
            // Keep the entire WebView inside system bars, cutouts and keyboard.
            // CSS padding scrolls away and cannot protect the status bar area.
            view.setPadding(left, top, right, bottom);
            return insets;
        });
        web = new WebView(this);
        WebView.setWebContentsDebuggingEnabled(BuildConfig.DEBUG);
        layout.addView(web, new FrameLayout.LayoutParams(-1, -1));
        applySystemTheme(false);
        boot = new FrameLayout(this);
        boot.setBackground(new GradientDrawable(GradientDrawable.Orientation.TL_BR,
            new int[]{Color.rgb(230, 246, 240), Color.rgb(245, 247, 250), Color.rgb(232, 239, 250)}));
        LinearLayout card = new LinearLayout(this);
        card.setOrientation(LinearLayout.VERTICAL);
        card.setGravity(Gravity.CENTER_HORIZONTAL);
        card.setPadding(dp(28), dp(36), dp(28), dp(28));
        GradientDrawable surface = new GradientDrawable();
        surface.setColor(Color.argb(245, 255, 255, 255)); surface.setCornerRadius(dp(28));
        surface.setStroke(dp(1), Color.WHITE);
        card.setBackground(surface); card.setElevation(dp(8));
        int width = Math.min(dp(340), getResources().getDisplayMetrics().widthPixels - dp(40));
        FrameLayout.LayoutParams cardParams = new FrameLayout.LayoutParams(width, -2, Gravity.CENTER);
        boot.addView(card, cardParams);
        ImageView logo = new ImageView(this);
        logo.setImageResource(R.drawable.app_icon);
        card.addView(logo, new LinearLayout.LayoutParams(dp(68), dp(68)));
        TextView tag = new TextView(this);
        tag.setText("你的本地 AI 网关"); tag.setTextSize(12); tag.setTextColor(Color.rgb(20, 134, 101));
        tag.setGravity(Gravity.CENTER);
        tag.setPadding(0, dp(20), 0, 0); card.addView(tag);
        TextView brand = new TextView(this);
        brand.setText("wb2api"); brand.setTextSize(24); brand.setTypeface(null, Typeface.BOLD);
        brand.setTextColor(Color.rgb(22, 24, 29)); brand.setGravity(Gravity.CENTER);
        brand.setPadding(0, dp(6), 0, dp(26)); card.addView(brand);
        bootTitle = new TextView(this);
        setStartupText(bootTitle, "正在打开工作台"); bootTitle.setTextSize(16);
        bootTitle.setGravity(Gravity.CENTER);
        bootTitle.setTextColor(Color.rgb(22, 24, 29)); card.addView(bootTitle);
        status = new TextView(this);
        setStartupText(status, "正在准备本地服务\n就绪后将自动进入工作台");
        status.setTextSize(13); status.setGravity(Gravity.CENTER);
        status.setTextColor(Color.rgb(101, 105, 112)); status.setLineSpacing(dp(5), 1);
        status.setPadding(0, dp(12), 0, dp(20)); card.addView(status);
        progress = new ProgressBar(this);
        progress.setIndeterminateTintList(android.content.res.ColorStateList.valueOf(Color.rgb(20, 134, 101)));
        card.addView(progress, new LinearLayout.LayoutParams(dp(28), dp(28)));
        restart = new Button(this);
        setStartupText(restart, "启动服务");
        restart.setTextColor(Color.WHITE);
        GradientDrawable action = new GradientDrawable();
        action.setColor(Color.rgb(20, 134, 101)); action.setCornerRadius(dp(12));
        restart.setBackground(action); restart.setVisibility(View.GONE);
        restart.setOnClickListener(view -> {
            if (LocalApiService.origin != null && !LocalApiService.starting) loadWorkbench();
            else startLocalService(true);
        });
        card.addView(restart, new LinearLayout.LayoutParams(-1, dp(48)));
        details = new Button(this);
        details.setText("启动诊断 · 复制或导出"); details.setTextSize(12); details.setTextColor(Color.rgb(20, 134, 101));
        details.setBackgroundColor(Color.TRANSPARENT); details.setVisibility(View.GONE);
        details.setOnClickListener(view -> showStartupDiagnostic());
        card.addView(details);
        TextView footnote = new TextView(this);
        footnote.setText("本地运行  ·  无需登录管理台\n首次启动可能稍慢，请保留账号数据");
        footnote.setTextSize(11); footnote.setGravity(Gravity.CENTER); footnote.setLineSpacing(dp(5), 1);
        footnote.setTextColor(Color.rgb(130, 139, 148)); footnote.setPadding(0, dp(22), 0, 0); card.addView(footnote);
        layout.addView(boot, new FrameLayout.LayoutParams(-1, -1));
        setContentView(layout);
        layout.requestApplyInsets();
        web.getSettings().setJavaScriptEnabled(true);
        web.getSettings().setDomStorageEnabled(true);
        // Local assets change with APK upgrades, even while the origin stays fixed.
        web.getSettings().setCacheMode(WebSettings.LOAD_NO_CACHE);
        web.getSettings().setAllowFileAccess(false);
        web.getSettings().setAllowContentAccess(true);
        web.getSettings().setSupportMultipleWindows(true);
        web.getSettings().setJavaScriptCanOpenWindowsAutomatically(false);
        web.getSettings().setMixedContentMode(WebSettings.MIXED_CONTENT_NEVER_ALLOW);
        web.addJavascriptInterface(new MobileBridge(), "WBAndroid");
        web.setWebViewClient(new WebViewClient() {
            @Override public void onPageStarted(WebView view, String url, android.graphics.Bitmap icon) {
                bridgeTrusted = isAdmin(url);
                if (bridgeTrusted) {
                    pageGeneration++; workbenchReady = false; checkingPage = false; pageError = null;
                    pageStartedAt = SystemClock.elapsedRealtime();
                    showStartupPage(); progress.setVisibility(View.VISIBLE);
                    setStartupText(bootTitle, "正在加载工作台"); setStartupText(status, "正在确认页面初始化…");
                    restart.setVisibility(View.GONE); details.setVisibility(View.GONE);
                }
            }
            @Override public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest request) {
                return route(request.getUrl());
            }
            @Override public void onPageFinished(WebView view, String url) {
                if (isAdmin(url)) checkWorkbench();
            }
            @Override public void onPageCommitVisible(WebView view, String url) {
                if (isAdmin(url)) checkWorkbench();
            }
            @Override public void onReceivedError(WebView view, WebResourceRequest request, WebResourceError error) {
                if (request.isForMainFrame()) {
                    pageError = "WebView " + error.getErrorCode() + ": " + error.getDescription();
                    setStartupText(bootTitle, "工作台连接失败"); setStartupText(status, "请点击重试重新连接本地服务。");
                    progress.setVisibility(View.GONE); setStartupText(restart, "重新连接"); restart.setVisibility(View.VISIBLE); details.setVisibility(View.VISIBLE);
                    showStartupPage();
                }
            }
        });
        web.setWebChromeClient(new WebChromeClient() {
            @Override public boolean onConsoleMessage(ConsoleMessage message) {
                if (message.messageLevel() == ConsoleMessage.MessageLevel.ERROR && isAdmin(web.getUrl())) {
                    String text = message.message();
                    // Retain script locations, never console values or tokens.
                    pageError = "脚本执行失败：" + Uri.parse(message.sourceId()).getPath() + ":" + message.lineNumber() +
                        (text.startsWith("Uncaught SyntaxError") ? "（WebView 不支持页面语法，请更新系统 WebView）" : "");
                }
                return true;
            }
            @Override public boolean onCreateWindow(WebView view, boolean dialog, boolean userGesture, Message message) {
                if (!userGesture) return false;
                WebView popup = new WebView(MainActivity.this);
                popup.setWebViewClient(new WebViewClient() {
                    private boolean opened;
                    private void open(String url) { if (!opened) { opened = true; external(Uri.parse(url)); popup.destroy(); } }
                    @Override public boolean shouldOverrideUrlLoading(WebView v, WebResourceRequest r) { open(r.getUrl().toString()); return true; }
                    @Override public void onPageStarted(WebView v, String url, android.graphics.Bitmap icon) { if (!"about:blank".equals(url)) open(url); }
                });
                ((WebView.WebViewTransport) message.obj).setWebView(popup); message.sendToTarget(); return true;
            }
            @Override public boolean onShowFileChooser(WebView view, ValueCallback<Uri[]> callback, FileChooserParams params) {
                if (upload != null) upload.onReceiveValue(null);
                upload = callback;
                Intent intent = new Intent(Intent.ACTION_GET_CONTENT).setType("*/*").addCategory(Intent.CATEGORY_OPENABLE);
                intent.putExtra(Intent.EXTRA_ALLOW_MULTIPLE, params.getMode() == FileChooserParams.MODE_OPEN_MULTIPLE);
                try { startActivityForResult(Intent.createChooser(intent, "选择账号备份"), 10); return true; }
                catch (ActivityNotFoundException e) { upload.onReceiveValue(null); upload = null; return false; }
            }
        });
        if (Build.VERSION.SDK_INT >= 33 && checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED)
            requestPermissions(new String[]{Manifest.permission.POST_NOTIFICATIONS}, 11);
        startLocalService();
        handler.post(waitForServer);
    }

    private void startLocalService() {
        startLocalService(false);
    }

    private void startLocalService(boolean recover) {
        Intent service = new Intent(this, LocalApiService.class);
        if (recover) service.setAction("restart");
        try {
            if (Build.VERSION.SDK_INT >= 26) startForegroundService(service); else startService(service);
        } catch (RuntimeException e) {
            LocalApiService.starting = false;
            LocalApiService.error = "系统未能启动前台服务：" + e.getMessage();
        }
    }

    public class MobileBridge {
        @JavascriptInterface public void setTheme(String theme) {
            if (bridgeTrusted) handler.post(() -> { if (!destroyed) applySystemTheme("dark".equals(theme)); });
        }
        @JavascriptInterface public String sessionToken() {
            return bridgeTrusted && LocalApiService.token != null ? LocalApiService.token : "";
        }
        @JavascriptInterface public synchronized boolean saveText(String request, String filename, String text, String mime) {
            if (!bridgeTrusted || destroyed || request == null || !request.matches("[0-9]{1,10}") || exportRequest != null || exportWriting || text == null) return false;
            byte[] bytes = text.getBytes(StandardCharsets.UTF_8);
            if (bytes.length > 32 * 1024 * 1024) return false;
            String safe = filename == null ? "wb2api-export.info" : filename.replaceAll("[\\\\/:*?\"<>|\\p{Cntrl}]", "_");
            if (safe.length() > 120) safe = safe.substring(0, 120);
            if (!safe.endsWith(".info") && !safe.endsWith(".json") && !safe.endsWith(".jsonl") && !safe.endsWith(".csv") && !safe.endsWith(".txt")) safe += ".info";
            exportRequest = request;
            exportBytes = bytes;
            final String title = safe;
            final String type = "text/csv".equals(mime) ? "text/csv" : "application/x-ndjson".equals(mime) ? "application/x-ndjson" : "text/plain".equals(mime) ? "text/plain" : "application/json";
            handler.post(() -> {
                if (destroyed) return;
                Intent intent = new Intent(Intent.ACTION_CREATE_DOCUMENT).addCategory(Intent.CATEGORY_OPENABLE)
                    .setType(type).putExtra(Intent.EXTRA_TITLE, title);
                try { startActivityForResult(intent, 12); }
                catch (RuntimeException e) { finishExport(false, "无法打开系统文件保存器"); }
            });
            return true;
        }
        @JavascriptInterface public String status() {
            if (!bridgeTrusted) return "{}";
            JSONObject data = new JSONObject();
            try {
                android.app.NotificationManager manager = getSystemService(android.app.NotificationManager.class);
                boolean notifications = manager.areNotificationsEnabled();
                if (Build.VERSION.SDK_INT >= 26) {
                    android.app.NotificationChannel channel = manager.getNotificationChannel("local-api");
                    notifications &= channel != null && channel.getImportance() != android.app.NotificationManager.IMPORTANCE_NONE;
                }
                PowerManager power = (PowerManager) getSystemService(POWER_SERVICE);
                data.put("registered", LocalApiService.registered);
                data.put("starting", LocalApiService.starting);
                data.put("last_healthy", LocalApiService.lastHealthy);
                data.put("notifications", notifications);
                data.put("battery_unrestricted", power.isIgnoringBatteryOptimizations(getPackageName()));
                android.app.ActivityManager activity = (android.app.ActivityManager) getSystemService(ACTIVITY_SERVICE);
                data.put("background_restricted", Build.VERSION.SDK_INT >= 28 && activity.isBackgroundRestricted());
                data.put("power_save", power.isPowerSaveMode());
                data.put("wake_lock", LocalApiService.wakeLockHeld());
                data.put("origin", LocalApiService.origin);
                data.put("error", LocalApiService.error);
            } catch (Exception ignored) { }
            return data.toString();
        }
        @JavascriptInterface public void restartService() {
            if (bridgeTrusted) handler.post(() -> { if (!destroyed) startLocalService(true); });
        }
        @JavascriptInterface public void openSystemSettings(String section) {
            if (!bridgeTrusted) return;
            handler.post(() -> {
                if (destroyed) return;
                Intent intent;
                if ("notifications".equals(section) && Build.VERSION.SDK_INT >= 26) {
                    intent = new Intent(android.provider.Settings.ACTION_APP_NOTIFICATION_SETTINGS)
                        .putExtra(android.provider.Settings.EXTRA_APP_PACKAGE, getPackageName());
                } else if ("battery".equals(section)) {
                    PowerManager power = (PowerManager) getSystemService(POWER_SERVICE);
                    if (power.isIgnoringBatteryOptimizations(getPackageName())) {
                        intent = new Intent(android.provider.Settings.ACTION_IGNORE_BATTERY_OPTIMIZATION_SETTINGS);
                    } else {
                        intent = new Intent(android.provider.Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS,
                                            Uri.parse("package:" + getPackageName()));
                    }
                } else {
                    intent = new Intent(android.provider.Settings.ACTION_APPLICATION_DETAILS_SETTINGS,
                                        Uri.parse("package:" + getPackageName()));
                }
                try { startActivity(intent); }
                catch (RuntimeException e) {
                    try { startActivity(new Intent(android.provider.Settings.ACTION_APPLICATION_DETAILS_SETTINGS,
                                                  Uri.parse("package:" + getPackageName()))); }
                    catch (RuntimeException ignored) { Toast.makeText(MainActivity.this, "无法打开系统设置", Toast.LENGTH_SHORT).show(); }
                }
            });
        }
    }

    private int dp(int value) { return Math.round(value * getResources().getDisplayMetrics().density); }

    private boolean isAdmin(String url) {
        if (loadedOrigin == null || url == null) return false;
        Uri uri = Uri.parse(url);
        return "http".equals(uri.getScheme()) && "127.0.0.1".equals(uri.getHost())
            && uri.getPort() == Uri.parse(loadedOrigin).getPort() && "/admin".equals(uri.getPath());
    }
    private boolean route(Uri uri) {
        if (isAdmin(uri.toString())) return false;
        external(uri); return true;
    }
    private void external(Uri uri) {
        if (!"https".equals(uri.getScheme()) && !"http".equals(uri.getScheme())) return;
        try { startActivity(new Intent(Intent.ACTION_VIEW, uri).addCategory(Intent.CATEGORY_BROWSABLE)); }
        catch (ActivityNotFoundException e) { Toast.makeText(this, "请先安装浏览器", Toast.LENGTH_SHORT).show(); }
    }
    @Override protected void onResume() {
        super.onResume();
        // Re-register the foreground service even when Python left an old origin in memory.
        if (web != null) startLocalService();
        if (web != null) web.evaluateJavascript("window.dispatchEvent(new Event('wb-shell-resume'))", null);
        if (LocalApiService.origin != null && Python.isStarted()) new Thread(() -> {
            try {
                String refreshed = Python.getInstance().getModule("android_runtime").callAttr("session_token").toString();
                if (!refreshed.isEmpty()) {
                    LocalApiService.token = refreshed;
                    handler.post(() -> {
                        if (!destroyed && isAdmin(web.getUrl())) web.evaluateJavascript("window.WBMobile && WBMobile.initialize(" + JSONObject.quote(refreshed) + ")", null);
                    });
                }
            } catch (Exception ignored) { }
        }, "workbuddy-session-refresh").start();
    }
    @Override protected void onActivityResult(int request, int result, Intent data) {
        super.onActivityResult(request, result, data);
        if (request == 13) {
            final byte[] bytes = startupExport; startupExport = null;
            if (result == RESULT_OK && data != null && data.getData() != null && bytes != null) {
                final Uri uri = data.getData();
                new Thread(() -> {
                    boolean ok = false;
                    try (OutputStream stream = getContentResolver().openOutputStream(uri)) {
                        if (stream != null) { stream.write(bytes); ok = true; }
                    } catch (Exception ignored) { }
                    final boolean saved = ok;
                    handler.post(() -> Toast.makeText(this, saved ? "启动诊断已保存" : "保存失败，请复制诊断", Toast.LENGTH_LONG).show());
                }, "workbuddy-startup-export").start();
            }
            return;
        }
        if (request == 10 && upload != null) {
            Uri[] selected = null;
            if (result == RESULT_OK && data != null && data.getClipData() != null) {
                ClipData clips = data.getClipData();
                selected = new Uri[clips.getItemCount()];
                for (int i = 0; i < clips.getItemCount(); i++) selected[i] = clips.getItemAt(i).getUri();
            } else selected = WebChromeClient.FileChooserParams.parseResult(result, data);
            upload.onReceiveValue(selected); upload = null;
        }
        if (request == 12 && exportRequest != null) {
            if (result != RESULT_OK || data == null || data.getData() == null) {
                finishExport(false, "已取消保存");
                return;
            }
            final Uri target = data.getData();
            final byte[] content = exportBytes;
            exportWriting = true;
            new Thread(() -> {
                boolean success = false;
                try (OutputStream output = getContentResolver().openOutputStream(target, "wt")) {
                    if (output == null || content == null) throw new java.io.IOException("No writable document");
                    output.write(content);
                    output.flush();
                    success = true;
                } catch (Exception e) {
                    success = false;
                    android.util.Log.e("WorkBuddyExport", "Document write failed: " + e.getClass().getSimpleName());
                }
                final boolean saved = success;
                handler.post(() -> finishExport(saved, saved ? "文件已保存" : "文件写入失败，请更换保存位置"));
            }, "workbuddy-file-export").start();
        }
    }
    private void finishExport(boolean success, String message) {
        String id = exportRequest;
        exportRequest = null; exportBytes = null; exportWriting = false;
        if (!destroyed && bridgeTrusted && id != null)
            web.evaluateJavascript("window.WBMobile && WBMobile.saveResult(" + JSONObject.quote(id) + "," + success + "," + JSONObject.quote(message) + ")", null);
    }
    @Override public void onBackPressed() {
        if (web != null && isAdmin(web.getUrl())) web.evaluateJavascript("if(document.querySelector('dialog[open]'))document.querySelector('dialog[open]').close();else if(!document.getElementById('modal').classList.contains('hidden'))closeModal();else closeSideMenu();", null);
        else super.onBackPressed();
    }
    @Override protected void onDestroy() {
        destroyed = true; handler.removeCallbacksAndMessages(null);
        if (boot != null && web != null) cancelStartupTransition();
        bridgeTrusted = false;
        if (upload != null) upload.onReceiveValue(null);
        if (web != null) web.destroy();
        super.onDestroy();
    }
}
