package xhs;

import android.accessibilityservice.AccessibilityServiceInfo;
import android.app.UiAutomation;
import android.graphics.Rect;
import android.os.Bundle;
import android.os.HandlerThread;
import android.os.Looper;
import android.util.Xml;
import android.view.accessibility.AccessibilityNodeInfo;
import android.view.accessibility.AccessibilityWindowInfo;
import java.io.FileOutputStream;
import java.lang.reflect.Constructor;
import java.util.ArrayList;
import java.util.List;
import org.xmlpull.v1.XmlSerializer;

/** Runs under the existing Shizuku shell identity; no idle wait on animated pages. */
public final class UiDump {
    private static int count;
    private static int emitted;

    public static void main(String[] args) throws Exception {
        if (args.length == 2 && args[0].equals("--set-text")) {
            setText(args[1]);
            return;
        }
        if (args.length != 1) throw new IllegalArgumentException("output path required");
        if (args[0].equals("--resume-xhs")) {
            resumeApp("com.xingin.xhs", "Open a Xiaohongshu work before starting this task");
            return;
        }
        if (args[0].equals("--resume-wechat")) {
            resumeApp("com.tencent.mm", "Open WeChat before starting this task");
            return;
        }
        HandlerThread thread = new HandlerThread("xhs-hierarchy");
        thread.start();
        UiAutomation automation = null;
        boolean connected = false;
        try {
            Class<?> connection = Class.forName("android.app.UiAutomationConnection");
            Class<?> binder = Class.forName("android.app.IUiAutomationConnection");
            Constructor<UiAutomation> constructor = UiAutomation.class.getDeclaredConstructor(Looper.class, binder);
            constructor.setAccessible(true);
            automation = constructor.newInstance(thread.getLooper(), connection.getDeclaredConstructor().newInstance());
            UiAutomation.class.getMethod("connect", int.class).invoke(automation, UiAutomation.FLAG_DONT_SUPPRESS_ACCESSIBILITY_SERVICES);
            connected = true;
            AccessibilityServiceInfo info = automation.getServiceInfo();
            info.flags |= AccessibilityServiceInfo.FLAG_REPORT_VIEW_IDS
                | AccessibilityServiceInfo.FLAG_INCLUDE_NOT_IMPORTANT_VIEWS
                | AccessibilityServiceInfo.FLAG_RETRIEVE_INTERACTIVE_WINDOWS;
            automation.setServiceInfo(info);
            AccessibilityNodeInfo root = null;
            for (int attempt = 0; attempt < 6; attempt++) {
                if (root != null) root.recycle();
                root = automation.getRootInActiveWindow();
                if (root != null && (root.isVisibleToUser() || root.getChildCount() > 0)) break;
                if (attempt < 5) Thread.sleep(100);
            }
            if (root == null) throw new IllegalStateException("No active window");
            try (FileOutputStream output = new FileOutputStream(args[0])) {
                XmlSerializer xml = Xml.newSerializer();
                xml.setOutput(output, "UTF-8");
                xml.startDocument("UTF-8", true);
                xml.startTag("", "hierarchy");
                root.refresh();
                write(xml, root, 0);
                // Some devices first expose an empty active-root placeholder.
                // Try only active windows, never a background app's tree.
                if (emitted == 0) {
                    for (AccessibilityWindowInfo window : automation.getWindows()) {
                        try {
                            if (!window.isActive()) continue;
                            AccessibilityNodeInfo windowRoot = window.getRoot();
                            if (windowRoot != null) {
                                try { write(xml, windowRoot, 0); } finally { windowRoot.recycle(); }
                            }
                        } finally { window.recycle(); }
                    }
                }
                xml.endTag("", "hierarchy");
                xml.endDocument();
                System.out.println("rootPackage=" + root.getPackageName() + " rootClass=" + root.getClassName()
                    + " rootChildren=" + root.getChildCount() + " visited=" + count + " exported=" + emitted);
            } finally {
                root.recycle();
            }
        } finally {
            if (connected) UiAutomation.class.getMethod("disconnect").invoke(automation);
            thread.quitSafely();
        }
    }

    private static void setText(String value) throws Exception {
        if (value == null || value.trim().isEmpty()) {
            throw new IllegalArgumentException("non-empty text required");
        }
        HandlerThread thread = new HandlerThread("xhs-set-text");
        thread.start();
        UiAutomation automation = null;
        boolean connected = false;
        try {
            Class<?> connection = Class.forName("android.app.UiAutomationConnection");
            Class<?> binder = Class.forName("android.app.IUiAutomationConnection");
            Constructor<UiAutomation> constructor = UiAutomation.class.getDeclaredConstructor(Looper.class, binder);
            constructor.setAccessible(true);
            automation = constructor.newInstance(thread.getLooper(), connection.getDeclaredConstructor().newInstance());
            UiAutomation.class.getMethod("connect", int.class).invoke(automation, UiAutomation.FLAG_DONT_SUPPRESS_ACCESSIBILITY_SERVICES);
            connected = true;
            AccessibilityServiceInfo info = automation.getServiceInfo();
            info.flags |= AccessibilityServiceInfo.FLAG_REPORT_VIEW_IDS
                | AccessibilityServiceInfo.FLAG_INCLUDE_NOT_IMPORTANT_VIEWS
                | AccessibilityServiceInfo.FLAG_RETRIEVE_INTERACTIVE_WINDOWS;
            automation.setServiceInfo(info);
            AccessibilityNodeInfo root = null;
            for (int attempt = 0; attempt < 10 && root == null; attempt++) {
                root = automation.getRootInActiveWindow();
                if (root == null) Thread.sleep(100);
            }
            List<AccessibilityNodeInfo> edits = new ArrayList<>();
            try {
                if (root != null) {
                    collectEdits(root, edits, 0);
                }
                if (edits.isEmpty()) {
                    AccessibilityNodeInfo focused = automation.findFocus(AccessibilityNodeInfo.FOCUS_INPUT);
                    if (focused != null) {
                        if (focused.isEditable() || "android.widget.EditText".equals(text(focused.getClassName()))) {
                            edits.add(focused);
                        } else {
                            focused.recycle();
                        }
                    }
                }
                if (edits.isEmpty()) {
                    for (AccessibilityWindowInfo window : automation.getWindows()) {
                        try {
                            if (!window.isActive()) continue;
                            AccessibilityNodeInfo windowRoot = window.getRoot();
                            if (windowRoot != null) {
                                try { collectEdits(windowRoot, edits, 0); } finally { windowRoot.recycle(); }
                            }
                        } finally { window.recycle(); }
                    }
                }
                if (edits.size() == 1) {
                    AccessibilityNodeInfo edit = edits.get(0);
                    Bundle arguments = new Bundle();
                    arguments.putCharSequence(AccessibilityNodeInfo.ACTION_ARGUMENT_SET_TEXT_CHARSEQUENCE, value);
                    if (edit.performAction(AccessibilityNodeInfo.ACTION_SET_TEXT, arguments)) {
                        System.out.println("text_set");
                        return;
                    }
                } else if (edits.size() > 1) {
                    throw new IllegalStateException("Expected at most one visible EditText, found " + edits.size());
                }
                pasteViaClipboard(value);
                System.out.println("text_set");
            } finally {
                for (AccessibilityNodeInfo edit : edits) {
                    edit.recycle();
                }
                if (root != null) root.recycle();
            }
        } finally {
            if (connected) UiAutomation.class.getMethod("disconnect").invoke(automation);
            thread.quitSafely();
        }
    }

    private static void pasteViaClipboard(String value) throws Exception {
        android.content.ClipData clip = android.content.ClipData.newPlainText("text", value);
        Class<?> sm = Class.forName("android.os.ServiceManager");
        android.os.IBinder binder = (android.os.IBinder) sm.getMethod("getService", String.class).invoke(null, "clipboard");
        Object clipboard = Class.forName("android.content.IClipboard$Stub")
            .getMethod("asInterface", android.os.IBinder.class).invoke(null, binder);
        boolean clipSet = false;
        for (java.lang.reflect.Method m : clipboard.getClass().getMethods()) {
            if (!m.getName().equals("setPrimaryClip")) continue;
            Class<?>[] p = m.getParameterTypes();
            try {
                if (p.length == 2 && p[0] == android.content.ClipData.class && p[1] == String.class) {
                    m.invoke(clipboard, clip, "com.android.shell");
                    clipSet = true;
                    break;
                } else if (p.length == 3 && p[0] == android.content.ClipData.class && p[1] == String.class && p[2] == int.class) {
                    m.invoke(clipboard, clip, "com.android.shell", 0);
                    clipSet = true;
                    break;
                } else if (p.length == 4 && p[0] == android.content.ClipData.class && p[1] == String.class && p[2] == String.class && p[3] == int.class) {
                    m.invoke(clipboard, clip, "com.android.shell", null, 0);
                    clipSet = true;
                    break;
                } else if (p.length == 5 && p[0] == android.content.ClipData.class && p[1] == String.class && p[2] == String.class && p[3] == int.class && p[4] == int.class) {
                    m.invoke(clipboard, clip, "com.android.shell", null, 0, 0);
                    clipSet = true;
                    break;
                }
            } catch (Exception ignored) {
            }
        }
        if (!clipSet) {
            throw new IllegalStateException("Could not set system clipboard for paste fallback");
        }
        Process proc = new ProcessBuilder("input", "keyevent", "279").start();
        if (proc.waitFor() != 0) {
            throw new IllegalStateException("KEYCODE_PASTE command failed");
        }
    }

    private static void collectEdits(AccessibilityNodeInfo node, List<AccessibilityNodeInfo> edits, int depth) {
        if (depth > 80) return;
        if (!node.isVisibleToUser()) return;
        Rect box = new Rect();
        node.getBoundsInScreen(box);
        if (box.isEmpty()) return;
        String pkg = text(node.getPackageName());
        if (("com.xingin.xhs".equals(pkg) || "com.tencent.mm".equals(pkg))
                && "android.widget.EditText".equals(text(node.getClassName()))
                && node.isEnabled()) {
            edits.add(AccessibilityNodeInfo.obtain(node));
        }
        for (int i = 0; i < node.getChildCount(); i++) {
            AccessibilityNodeInfo child = node.getChild(i);
            if (child == null) continue;
            try {
                collectEdits(child, edits, depth + 1);
            } finally {
                child.recycle();
            }
        }
    }

    private static void resumeApp(String packageName, String missingMessage) throws Exception {
        Object service = Class.forName("android.app.ActivityTaskManager").getMethod("getService").invoke(null);
        java.lang.reflect.Method tasks = null;
        for (java.lang.reflect.Method method : service.getClass().getMethods()) {
            if (method.getName().equals("getTasks")) { tasks = method; break; }
        }
        if (tasks == null) throw new IllegalStateException("Running tasks API unavailable");
        Class<?>[] types = tasks.getParameterTypes();
        Object[] args = new Object[types.length];
        for (int i = 0; i < types.length; i++) {
            if (types[i] == int.class) args[i] = i == 0 ? 100 : 0;
            else if (types[i] == boolean.class) args[i] = false;
            else throw new IllegalStateException("Unsupported running tasks API");
        }
        for (Object item : (java.util.List<?>) tasks.invoke(service, args)) {
            android.app.ActivityManager.RunningTaskInfo task = (android.app.ActivityManager.RunningTaskInfo) item;
            if (task.topActivity == null || !packageName.equals(task.topActivity.getPackageName())
                    || task.getClass().getField("displayId").getInt(task) != 0) continue;
            service.getClass().getMethod("moveTaskToFront", Class.forName("android.app.IApplicationThread"),
                    String.class, int.class, int.class, android.os.Bundle.class)
                .invoke(service, null, "com.android.shell", task.taskId, 0, null);
            System.out.println("resumed");
            return;
        }
        throw new IllegalStateException(missingMessage);
    }

    private static String text(CharSequence value) {
        return value == null ? "" : value.toString();
    }

    private static void write(XmlSerializer xml, AccessibilityNodeInfo node, int depth) throws Exception {
        if (depth > 80 || ++count > 10000) throw new IllegalStateException("Hierarchy limit exceeded");
        Rect box = new Rect();
        node.getBoundsInScreen(box);
        // Some vendor containers are invisible/empty while their descendants
        // are visible. Filter the container itself, never prune its subtree.
        boolean visible = node.isVisibleToUser() && !box.isEmpty();
        if (visible) {
            emitted++;
            xml.startTag("", "node");
            xml.attribute("", "text", node.isPassword() ? "" : text(node.getText()));
            xml.attribute("", "hint", node.isPassword() ? "" : text(node.getHintText()));
            xml.attribute("", "content-desc", node.isPassword() ? "" : text(node.getContentDescription()));
            xml.attribute("", "resource-id", text(node.getViewIdResourceName()));
            xml.attribute("", "class", text(node.getClassName()));
            xml.attribute("", "package", text(node.getPackageName()));
            xml.attribute("", "bounds", "[" + box.left + "," + box.top + "][" + box.right + "," + box.bottom + "]");
            xml.attribute("", "clickable", Boolean.toString(node.isClickable()));
            xml.attribute("", "enabled", Boolean.toString(node.isEnabled()));
            xml.attribute("", "selected", Boolean.toString(node.isSelected()));
            xml.attribute("", "scrollable", Boolean.toString(node.isScrollable()));
        }
        for (int i = 0; i < node.getChildCount(); i++) {
            AccessibilityNodeInfo child = node.getChild(i);
            if (child == null) continue;
            try { write(xml, child, depth + 1); } finally { child.recycle(); }
        }
        if (visible) xml.endTag("", "node");
    }
}
