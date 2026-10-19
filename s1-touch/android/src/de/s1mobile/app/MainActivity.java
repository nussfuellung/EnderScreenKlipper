package de.s1mobile.app;

import android.app.Activity;
import android.app.AlertDialog;
import android.app.DownloadManager;
import android.app.Notification;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.content.res.Configuration;
import android.graphics.Color;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.os.Environment;
import android.text.InputType;
import android.view.View;
import android.view.Window;
import android.webkit.DownloadListener;
import android.content.DialogInterface;
import android.webkit.CookieManager;
import android.webkit.JavascriptInterface;
import android.webkit.URLUtil;
import android.webkit.ValueCallback;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceError;
import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.EditText;
import android.widget.FrameLayout;
import android.widget.Toast;
import java.lang.reflect.Constructor;
import java.lang.reflect.Method;
import java.net.URLEncoder;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/** S1 Mobile – lädt die Handy-App vom eigenen Server (http, ohne HTTPS-Zwang). */
public class MainActivity extends Activity {
    private static final int REQ_FILE = 11, REQ_NOTIFY = 12;
    private static final String DEFAULT_URL = "http://192.168.188.71:8081";
    private static final Pattern PRINTABLES = Pattern.compile("https?://(?:www\\.)?printables\\.com/(?:[a-z]{2}/)?model/(\\d+)[^\\s]*", Pattern.CASE_INSENSITIVE);
    private WebView web;
    private ValueCallback<Uri[]> fileCb;
    private SharedPreferences prefs;
    private boolean dark;

    @Override
    protected void onCreate(Bundle b) {
        prefs = getSharedPreferences("s1mobile", MODE_PRIVATE);
        dark = (getResources().getConfiguration().uiMode & Configuration.UI_MODE_NIGHT_MASK) == Configuration.UI_MODE_NIGHT_YES;
        // WebView übernimmt "prefers-color-scheme" vom App-Theme
        setTheme(dark ? android.R.style.Theme_DeviceDefault_NoActionBar : android.R.style.Theme_DeviceDefault_Light_NoActionBar);
        super.onCreate(b);
        Window w = getWindow();
        int bg = dark ? Color.rgb(15, 16, 18) : Color.rgb(242, 243, 245);
        w.setStatusBarColor(bg);
        w.setNavigationBarColor(bg);
        if (!dark) w.getDecorView().setSystemUiVisibility(View.SYSTEM_UI_FLAG_LIGHT_STATUS_BAR);

        web = new WebView(this);
        web.setBackgroundColor(bg);
        FrameLayout root = new FrameLayout(this);
        root.addView(web);
        setContentView(root);

        WebSettings s = web.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);
        s.setDatabaseEnabled(true);
        s.setMediaPlaybackRequiresUserGesture(false);
        s.setMixedContentMode(WebSettings.MIXED_CONTENT_ALWAYS_ALLOW);
        s.setUserAgentString(s.getUserAgentString() + " S1MobileApp/1.0");
        CookieManager.getInstance().setAcceptCookie(true);
        web.addJavascriptInterface(new Bridge(), "S1Native");

        web.setWebViewClient(new WebViewClient() {
            // API 24+ (in android-23.jar noch nicht vorhanden → ohne @Override, wird zur Laufzeit trotzdem aufgerufen)
            public boolean shouldOverrideUrlLoading(WebView v, WebResourceRequest r) {
                return openExternalIfForeign(r.getUrl());
            }
            @SuppressWarnings("deprecation")
            @Override
            public boolean shouldOverrideUrlLoading(WebView v, String url) {
                return openExternalIfForeign(Uri.parse(url));
            }
            @Override
            public void onReceivedError(WebView v, WebResourceRequest r, WebResourceError e) {
                if (r.isForMainFrame()) showOffline(String.valueOf(e.getDescription()));
            }
            @SuppressWarnings("deprecation")
            @Override
            public void onReceivedError(WebView v, int code, String desc, String url) {
                if (url != null && url.equals(v.getUrl())) showOffline(desc);
            }
        });
        web.setWebChromeClient(new WebChromeClient() {
            @Override
            public boolean onShowFileChooser(WebView v, ValueCallback<Uri[]> cb, FileChooserParams p) {
                if (fileCb != null) fileCb.onReceiveValue(null);
                fileCb = cb;
                Intent i = new Intent(Intent.ACTION_GET_CONTENT);
                i.addCategory(Intent.CATEGORY_OPENABLE);
                i.setType("*/*");
                try {
                    startActivityForResult(Intent.createChooser(i, "Datei wählen"), REQ_FILE);
                } catch (Exception ex) {
                    fileCb = null;
                    return false;
                }
                return true;
            }
        });
        web.setDownloadListener(new DownloadListener() { @Override public void onDownloadStart(String url, String ua, String cd, String mime, long len) {
            try {
                DownloadManager.Request req = new DownloadManager.Request(Uri.parse(url));
                String name = URLUtil.guessFileName(url, cd, mime);
                req.setTitle(name);
                req.setNotificationVisibility(DownloadManager.Request.VISIBILITY_VISIBLE_NOTIFY_COMPLETED);
                req.setDestinationInExternalPublicDir(Environment.DIRECTORY_DOWNLOADS, name);
                ((DownloadManager) getSystemService(DOWNLOAD_SERVICE)).enqueue(req);
                Toast.makeText(MainActivity.this, "Download: " + name, Toast.LENGTH_SHORT).show();
            } catch (Exception ex) {
                startActivity(new Intent(Intent.ACTION_VIEW, Uri.parse(url)));
            }
        }});

        if (b != null) web.restoreState(b);
        else if (server() == null) askServer(true);
        else loadFor(getIntent());
    }

    private String server() { return prefs.getString("url", null); }

    private boolean openExternalIfForeign(Uri u) {
        String srv = server();
        if (srv == null || u == null) return false;
        Uri s = Uri.parse(srv);
        if (u.getHost() != null && u.getHost().equalsIgnoreCase(s.getHost())) return false;
        if ("about".equals(u.getScheme()) || "data".equals(u.getScheme()) || "blob".equals(u.getScheme())) return false;
        try { startActivity(new Intent(Intent.ACTION_VIEW, u)); } catch (Exception ignored) { }
        return true;
    }

    private void loadFor(Intent i) {
        String srv = server();
        if (srv == null) return;
        String shared = null;
        if (i != null) {
            if (Intent.ACTION_SEND.equals(i.getAction())) shared = i.getStringExtra(Intent.EXTRA_TEXT);
            else if (Intent.ACTION_VIEW.equals(i.getAction()) && i.getData() != null) shared = i.getData().toString();
            String tab = i.getStringExtra("tab");
            if (tab != null && shared == null) { web.loadUrl(srv + "/?tab=" + tab); return; }
        }
        if (shared != null) {
            Matcher m = PRINTABLES.matcher(shared);
            if (m.find()) {
                try {
                    web.loadUrl(srv + "/?url=" + URLEncoder.encode(m.group(0), "UTF-8"));
                    return;
                } catch (Exception ignored) { }
            }
            Toast.makeText(this, "Kein Printables-Link gefunden", Toast.LENGTH_SHORT).show();
        }
        if (web.getUrl() == null || web.getUrl().startsWith("data:")) web.loadUrl(srv + "/");
    }

    @Override
    protected void onNewIntent(Intent i) {
        super.onNewIntent(i);
        setIntent(i);
        loadFor(i);
    }

    private void askServer(boolean first) {
        final EditText in = new EditText(this);
        in.setInputType(InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_VARIATION_URI);
        in.setText(server() != null ? server() : DEFAULT_URL);
        in.setSelectAllOnFocus(true);
        int pad = (int) (20 * getResources().getDisplayMetrics().density);
        FrameLayout box = new FrameLayout(this);
        box.setPadding(pad, pad / 2, pad, 0);
        box.addView(in);
        AlertDialog.Builder d = new AlertDialog.Builder(this)
            .setTitle(first ? "Willkommen bei S1 Mobile" : "Server-Adresse")
            .setMessage("Adresse der Handy-App auf deinem Server (Port 8081):")
            .setView(box)
            .setPositiveButton("Verbinden", new DialogInterface.OnClickListener() { @Override public void onClick(DialogInterface dlg, int x) {
                String u = in.getText().toString().trim().replaceAll("/+$", "");
                if (!u.startsWith("http")) u = "http://" + u;
                prefs.edit().putString("url", u).apply();
                web.clearHistory();
                web.loadUrl(u + "/");
            }});
        if (!first) d.setNegativeButton("Abbrechen", null);
        d.setCancelable(!first).show();
    }

    private void showOffline(String why) {
        String srv = server() == null ? "" : server();
        String fg = dark ? "#eef0f3" : "#15171a", bg = dark ? "#0f1012" : "#f2f3f5", sub = dark ? "#9aa1ab" : "#6b7280";
        String html = "<html><head><meta name='viewport' content='width=device-width,initial-scale=1'></head>"
            + "<body style='margin:0;font-family:sans-serif;background:" + bg + ";color:" + fg + ";display:flex;align-items:center;justify-content:center;height:100vh;text-align:center'>"
            + "<div style='padding:24px'><div style='font-size:46px'>🖨️</div><h2>Server nicht erreichbar</h2>"
            + "<p style='color:" + sub + "'>" + esc(srv) + "<br><small>" + esc(why) + "</small></p>"
            + "<p style='color:" + sub + "'>Bist du im Heimnetz (oder per VPN verbunden)?</p>"
            + "<button onclick='location.href=\"" + esc(srv) + "/\"' style='height:46px;padding:0 22px;border-radius:23px;border:0;background:#1f6feb;color:#fff;font-size:16px'>Erneut versuchen</button><br><br>"
            + "<button onclick='S1Native.changeServer()' style='height:42px;padding:0 20px;border-radius:21px;border:1px solid " + sub + ";background:none;color:" + fg + ";font-size:15px'>Adresse ändern</button></div></body></html>";
        web.loadDataWithBaseURL(null, html, "text/html", "utf-8", null);
    }

    private static String esc(String s) { return s == null ? "" : s.replace("&", "&amp;").replace("<", "&lt;").replace("\"", "&quot;").replace("'", "&#39;"); }

    @Override
    protected void onActivityResult(int req, int res, Intent data) {
        if (req == REQ_FILE && fileCb != null) {
            Uri[] r = null;
            if (res == RESULT_OK && data != null) {
                if (data.getClipData() != null) {
                    r = new Uri[data.getClipData().getItemCount()];
                    for (int k = 0; k < r.length; k++) r[k] = data.getClipData().getItemAt(k).getUri();
                } else if (data.getData() != null) r = new Uri[]{data.getData()};
            }
            fileCb.onReceiveValue(r);
            fileCb = null;
            return;
        }
        super.onActivityResult(req, res, data);
    }

    @Override
    public void onBackPressed() {
        if (web.canGoBack()) web.goBack();
        else super.onBackPressed();
    }

    @Override protected void onSaveInstanceState(Bundle b) { super.onSaveInstanceState(b); web.saveState(b); }
    @Override protected void onResume() { super.onResume(); web.onResume(); web.evaluateJavascript("window.dispatchEvent(new Event('focus'))", null); }
    @Override protected void onPause() { web.onPause(); super.onPause(); }

    // ---------- Benachrichtigungen ----------
    private void ensureChannel() {
        if (Build.VERSION.SDK_INT < 26) return;
        try {
            Class<?> ch = Class.forName("android.app.NotificationChannel");
            Constructor<?> c = ch.getConstructor(String.class, CharSequence.class, int.class);
            Object chan = c.newInstance("print", "Druckstatus", 3);
            NotificationManager nm = (NotificationManager) getSystemService(NOTIFICATION_SERVICE);
            Method m = NotificationManager.class.getMethod("createNotificationChannel", ch);
            m.invoke(nm, chan);
        } catch (Exception ignored) { }
    }

    private void postNotification(String title, String body) {
        try {
            ensureChannel();
            Notification.Builder nb;
            if (Build.VERSION.SDK_INT >= 26) {
                nb = (Notification.Builder) Notification.Builder.class.getConstructor(Context.class, String.class).newInstance(this, "print");
            } else {
                nb = new Notification.Builder(this);
            }
            Intent open = new Intent(this, MainActivity.class).putExtra("tab", "printer").addFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP);
            PendingIntent pi = PendingIntent.getActivity(this, 0, open, PendingIntent.FLAG_UPDATE_CURRENT | 0x04000000 /* FLAG_IMMUTABLE */);
            nb.setSmallIcon(R.drawable.ic_notify).setContentTitle(title).setContentText(body).setAutoCancel(true).setContentIntent(pi);
            ((NotificationManager) getSystemService(NOTIFICATION_SERVICE)).notify(1, nb.build());
        } catch (Exception ignored) { }
    }

    private boolean notifyAllowed() {
        if (Build.VERSION.SDK_INT >= 33) return checkSelfPermission("android.permission.POST_NOTIFICATIONS") == 0;
        return true;
    }

    /** Wird von der Web-App aufgerufen (window.S1Native). */
    class Bridge {
        @JavascriptInterface public boolean isApp() { return true; }
        @JavascriptInterface public String version() { return "1.0"; }
        @JavascriptInterface public void notify(final String title, final String body) { runOnUiThread(new Runnable() { public void run() { postNotification(title, body); } }); }
        @JavascriptInterface public boolean notifyAllowed() { return MainActivity.this.notifyAllowed(); }
        @JavascriptInterface public void requestNotify() {
            runOnUiThread(new Runnable() { public void run() {
                ensureChannel();
                if (Build.VERSION.SDK_INT >= 33 && !MainActivity.this.notifyAllowed())
                    requestPermissions(new String[]{"android.permission.POST_NOTIFICATIONS"}, REQ_NOTIFY);
            }});
        }
        @JavascriptInterface public void changeServer() { runOnUiThread(new Runnable() { public void run() { askServer(false); } }); }
        @JavascriptInterface public String server() { return MainActivity.this.server(); }
    }
}
