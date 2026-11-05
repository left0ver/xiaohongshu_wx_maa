package xhs;

import android.accessibilityservice.AccessibilityServiceInfo;
import android.app.UiAutomation;
import android.graphics.Rect;
import android.os.HandlerThread;
import android.os.Looper;
import android.util.Xml;
import android.view.accessibility.AccessibilityNodeInfo;
import android.view.accessibility.AccessibilityWindowInfo;
import java.io.FileOutputStream;
import java.lang.reflect.Constructor;
import org.xmlpull.v1.XmlSerializer;

/** Runs under the existing Shizuku shell identity; no idle wait on animated pages. */
public final class UiDump {
    private static int count;
    private static int emitted;

    public static void main(String[] args) throws Exception {
        if (args.length != 1) throw new IllegalArgumentException("output path required");
        if (args[0].equals("--resume-xhs")) {
            resumeXhs();
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

    private static void resumeXhs() throws Exception {
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
            if (task.topActivity == null || !"com.xingin.xhs".equals(task.topActivity.getPackageName())
                    || task.getClass().getField("displayId").getInt(task) != 0) continue;
            service.getClass().getMethod("moveTaskToFront", Class.forName("android.app.IApplicationThread"),
                    String.class, int.class, int.class, android.os.Bundle.class)
                .invoke(service, null, "com.android.shell", task.taskId, 0, null);
            System.out.println("resumed");
            return;
        }
        throw new IllegalStateException("Open a Xiaohongshu work before starting this task");
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
