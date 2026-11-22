using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.Globalization;
using System.IO;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using System.Windows.Forms;

namespace BackgroundKeys
{
    internal static class Native
    {
        internal const uint KeyDown = 0x100, KeyUp = 0x101;
        internal delegate bool EnumWindow(IntPtr hwnd, IntPtr data);
        [DllImport("user32.dll")] internal static extern bool EnumWindows(EnumWindow callback, IntPtr data);
        [DllImport("user32.dll")] internal static extern bool IsWindowVisible(IntPtr hwnd);
        [DllImport("user32.dll")] internal static extern bool IsWindow(IntPtr hwnd);
        [DllImport("user32.dll")] internal static extern bool IsChild(IntPtr parent, IntPtr child);
        [DllImport("user32.dll", CharSet = CharSet.Unicode)] internal static extern int GetWindowText(IntPtr hwnd, StringBuilder text, int count);
        [DllImport("user32.dll")] internal static extern uint GetWindowThreadProcessId(IntPtr hwnd, out uint pid);
        [DllImport("user32.dll", SetLastError = true)] internal static extern bool PostMessage(IntPtr hwnd, uint message, IntPtr key, IntPtr flags);
        [DllImport("user32.dll")] internal static extern IntPtr GetKeyboardLayout(uint thread);
        [DllImport("user32.dll")] internal static extern uint MapVirtualKeyEx(uint code, uint mapType, IntPtr layout);
        [DllImport("user32.dll")] internal static extern bool GetGUIThreadInfo(uint thread, ref GuiInfo info);
        [DllImport("user32.dll")] internal static extern bool RegisterHotKey(IntPtr hwnd, int id, uint modifiers, uint key);
        [DllImport("user32.dll")] internal static extern bool UnregisterHotKey(IntPtr hwnd, int id);
        [DllImport("user32.dll")] internal static extern IntPtr GetForegroundWindow();
        [DllImport("user32.dll")] internal static extern bool SetProcessDPIAware();

        [StructLayout(LayoutKind.Sequential)]
        internal struct Rect { internal int Left, Top, Right, Bottom; }
        [StructLayout(LayoutKind.Sequential)]
        internal struct GuiInfo
        {
            internal int Size;
            internal uint Flags;
            internal IntPtr Active, Focus, Capture, MenuOwner, MoveSize, Caret;
            internal Rect CaretRect;
        }

        internal static IntPtr KeyFlags(Keys key, uint thread, bool up)
        {
            uint scan = MapVirtualKeyEx((uint)key, 4, GetKeyboardLayout(thread));
            uint flags = 1 | ((scan & 0xff) << 16);
            if ((scan & 0xff00) == 0xe000) flags |= 1u << 24;
            if (up) flags |= 0xc0000000;
            return new IntPtr(unchecked((int)flags));
        }
    }

    internal sealed class Target
    {
        internal IntPtr Handle;
        internal uint ProcessId, ThreadId;
        internal string Title, ProcessName;

        internal bool IsAlive()
        {
            uint pid;
            uint thread = Native.GetWindowThreadProcessId(Handle, out pid);
            return Native.IsWindow(Handle) && pid == ProcessId && thread == ThreadId;
        }

        internal IntPtr Receiver(bool useFocus)
        {
            if (!useFocus) return Handle;
            Native.GuiInfo info = new Native.GuiInfo();
            info.Size = Marshal.SizeOf(typeof(Native.GuiInfo));
            if (Native.GetGUIThreadInfo(ThreadId, ref info) && info.Focus != IntPtr.Zero &&
                (info.Focus == Handle || Native.IsChild(Handle, info.Focus))) return info.Focus;
            return Handle;
        }

        internal static List<Target> Enumerate()
        {
            List<Target> result = new List<Target>();
            uint own = (uint)Process.GetCurrentProcess().Id;
            Native.EnumWindows(delegate(IntPtr hwnd, IntPtr unused)
            {
                if (!Native.IsWindowVisible(hwnd)) return true;
                StringBuilder text = new StringBuilder(2048);
                Native.GetWindowText(hwnd, text, text.Capacity);
                if (String.IsNullOrWhiteSpace(text.ToString())) return true;
                uint pid;
                uint thread = Native.GetWindowThreadProcessId(hwnd, out pid);
                if (pid == own || pid == 0) return true;
                string name = "?";
                try { using (Process p = Process.GetProcessById((int)pid)) name = p.ProcessName; }
                catch (ArgumentException) { return true; }
                catch (System.ComponentModel.Win32Exception) { }
                result.Add(new Target { Handle = hwnd, ProcessId = pid, ThreadId = thread,
                    Title = text.ToString(), ProcessName = name });
                return true;
            }, IntPtr.Zero);
            result.Sort(delegate(Target a, Target b) { return StringComparer.CurrentCultureIgnoreCase.Compare(a.Title, b.Title); });
            return result;
        }
    }

    internal sealed class RunResult
    {
        internal string Message;
        internal bool Error;
    }

    internal sealed class Sender
    {
        private long rounds, deliveries;
        internal long Rounds { get { return Interlocked.Read(ref rounds); } }
        internal long Deliveries { get { return Interlocked.Read(ref deliveries); } }

        internal RunResult Run(List<Target> selected, Keys first, Keys second, int minMs, int maxMs,
            bool useFocus, CancellationToken token)
        {
            List<Target> active = new List<Target>(selected);
            Random random = new Random();
            Keys key = first;
            try
            {
                while (!token.IsCancellationRequested)
                {
                    active.RemoveAll(delegate(Target target) { return !target.IsAlive(); });
                    if (active.Count == 0) return new RunResult { Message = "Все выбранные окна закрыты. Работа остановлена." };
                    int delay = random.Next(minMs, maxMs + 1);
                    Stopwatch cycle = Stopwatch.StartNew();
                    List<KeyValuePair<Target, IntPtr>> pressed = new List<KeyValuePair<Target, IntPtr>>();
                    try
                    {
                        foreach (Target target in active)
                        {
                            if (token.IsCancellationRequested) break;
                            if (!target.IsAlive()) continue;
                            IntPtr receiver = target.Receiver(useFocus);
                            if (!Native.PostMessage(receiver, Native.KeyDown, (IntPtr)(int)key,
                                Native.KeyFlags(key, target.ThreadId, false)))
                                return Failure(target, Marshal.GetLastWin32Error());
                            pressed.Add(new KeyValuePair<Target, IntPtr>(target, receiver));
                            Interlocked.Increment(ref deliveries);
                        }
                        if (pressed.Count > 0)
                        {
                            Interlocked.Increment(ref rounds);
                            token.WaitHandle.WaitOne(35);
                        }
                    }
                    finally
                    {
                        RunResult releaseError = null;
                        foreach (KeyValuePair<Target, IntPtr> entry in pressed)
                        {
                            if (entry.Key.IsAlive() && Native.IsWindow(entry.Value))
                                if (!Native.PostMessage(entry.Value, Native.KeyUp, (IntPtr)(int)key,
                                    Native.KeyFlags(key, entry.Key.ThreadId, true)))
                                    releaseError = Failure(entry.Key, Marshal.GetLastWin32Error());
                        }
                        if (releaseError != null) throw new InvalidOperationException(releaseError.Message);
                    }
                    key = key == first ? second : first;
                    int remaining = Math.Max(0, delay - (int)cycle.ElapsedMilliseconds);
                    if (token.WaitHandle.WaitOne(remaining)) break;
                }
                return new RunResult { Message = "Остановлено. Для нового запуска нажмите «Старт»." };
            }
            catch (Exception ex) { return new RunResult { Error = true, Message = "Ошибка отправки: " + ex.Message }; }
        }

        private static RunResult Failure(Target target, int code)
        {
            string detail = code == 5 ? "Доступ запрещён: запустите утилиту с теми же правами, что и целевое приложение."
                : new System.ComponentModel.Win32Exception(code).Message;
            return new RunResult { Error = true, Message = "Отправка в «" + target.Title + "» не удалась. " + detail };
        }
    }

    internal sealed class KeyBox : TextBox
    {
        internal Keys Value;
        internal KeyBox(Keys initial)
        {
            ReadOnly = true;
            ShortcutsEnabled = false;
            TextAlign = HorizontalAlignment.Center;
            Width = 110;
            BackColor = Color.White;
            Assign(initial);
        }
        internal void Assign(Keys key) { Value = key; Text = NameOf(key); }
        internal static string NameOf(Keys key)
        {
            int value = (int)key;
            return value >= (int)Keys.D0 && value <= (int)Keys.D9 ? (value - (int)Keys.D0).ToString() : key.ToString();
        }
        protected override bool IsInputKey(Keys keyData) { return true; }
        protected override void OnKeyDown(KeyEventArgs e)
        {
            e.SuppressKeyPress = true;
            e.Handled = true;
            Keys key = e.KeyCode;
            if (e.Modifiers != Keys.None || key == Keys.F8 || key == Keys.ShiftKey || key == Keys.ControlKey ||
                key == Keys.Menu || key == Keys.LWin || key == Keys.RWin || key == Keys.None) return;
            Assign(key);
        }
    }

    internal sealed class MainForm : Form
    {
        private readonly ListView windows = new ListView();
        private readonly TextBox search = new TextBox();
        private readonly KeyBox first = new KeyBox(Keys.D3), second = new KeyBox(Keys.D4);
        private readonly NumericUpDown minimum = new NumericUpDown(), maximum = new NumericUpDown();
        private readonly Button refresh = new Button(), selectAll = new Button(), clear = new Button();
        private readonly Button start = new Button(), stop = new Button();
        private readonly CheckBox useFocus = new CheckBox();
        private readonly Label timerLabel = new Label(), countLabel = new Label(), status = new Label(), hotkeyLabel = new Label();
        private readonly System.Windows.Forms.Timer ticker = new System.Windows.Forms.Timer();
        private readonly Stopwatch elapsed = new Stopwatch();
        private readonly HashSet<string> checkedWindows = new HashSet<string>();
        private List<Target> available = new List<Target>();
        private CancellationTokenSource cancel;
        private Task<RunResult> task;
        private Sender sender;
        private bool rebuilding, closing, hotkey;
        private readonly bool previewMode;

        internal MainForm() : this(false) { }
        internal MainForm(bool preview)
        {
            previewMode = preview;
            Text = "Background Keys — фоновые нажатия";
            Font = new Font("Segoe UI", 10);
            StartPosition = FormStartPosition.CenterScreen;
            ClientSize = new Size(900, 700);
            MinimumSize = new Size(820, 700);
            AutoScaleMode = AutoScaleMode.Dpi;

            TableLayoutPanel root = new TableLayoutPanel { Dock = DockStyle.Fill, Padding = new Padding(18), ColumnCount = 1, RowCount = 8 };
            root.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100));
            root.RowStyles.Add(new RowStyle(SizeType.Absolute, 39));
            root.RowStyles.Add(new RowStyle(SizeType.Absolute, 40));
            root.RowStyles.Add(new RowStyle(SizeType.Percent, 100));
            root.RowStyles.Add(new RowStyle(SizeType.Absolute, 103));
            root.RowStyles.Add(new RowStyle(SizeType.Absolute, 34));
            root.RowStyles.Add(new RowStyle(SizeType.Absolute, 53));
            root.RowStyles.Add(new RowStyle(SizeType.Absolute, 62));
            root.RowStyles.Add(new RowStyle(SizeType.Absolute, 62));
            Controls.Add(root);
            root.Controls.Add(new Label { Text = "Выберите окна галочками. Нажатия отправляются во все отмеченные окна.", Dock = DockStyle.Fill, TextAlign = ContentAlignment.MiddleLeft }, 0, 0);

            FlowLayoutPanel toolbar = new FlowLayoutPanel { Dock = DockStyle.Fill, WrapContents = false };
            toolbar.Controls.Add(new Label { Text = "Поиск:", AutoSize = true, Margin = new Padding(0, 7, 6, 0) });
            search.Width = 255;
            search.TextChanged += delegate { RebuildList(); };
            toolbar.Controls.Add(search);
            SetupButton(refresh, "Обновить", 110, delegate { RefreshWindows(); });
            SetupButton(selectAll, "Выбрать все", 120, delegate { SetChecks(true); });
            SetupButton(clear, "Снять выбор", 120, delegate { checkedWindows.Clear(); SetChecks(false); });
            toolbar.Controls.AddRange(new Control[] { refresh, selectAll, clear });
            root.Controls.Add(toolbar, 0, 1);

            windows.Dock = DockStyle.Fill;
            windows.View = View.Details;
            windows.CheckBoxes = true;
            windows.FullRowSelect = true;
            windows.HideSelection = false;
            windows.Columns.Add("Окно", 500);
            windows.Columns.Add("Процесс", 180);
            windows.Columns.Add("PID", 90);
            windows.ItemChecked += delegate(object s, ItemCheckedEventArgs e)
            {
                if (rebuilding) return;
                string identity = Identity((Target)e.Item.Tag);
                if (e.Item.Checked) checkedWindows.Add(identity); else checkedWindows.Remove(identity);
                UpdateCounts();
            };
            root.Controls.Add(windows, 0, 2);

            TableLayoutPanel settings = new TableLayoutPanel { Dock = DockStyle.Fill, ColumnCount = 4, RowCount = 2, Padding = new Padding(0, 10, 0, 0) };
            for (int i = 0; i < 4; i++) settings.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 25));
            settings.RowStyles.Add(new RowStyle(SizeType.Absolute, 31));
            settings.RowStyles.Add(new RowStyle(SizeType.Absolute, 40));
            settings.Controls.Add(new Label { Text = "Первая клавиша", AutoSize = true }, 0, 0);
            settings.Controls.Add(new Label { Text = "Вторая клавиша", AutoSize = true }, 1, 0);
            settings.Controls.Add(new Label { Text = "Задержка от, сек.", AutoSize = true }, 2, 0);
            settings.Controls.Add(new Label { Text = "Задержка до, сек.", AutoSize = true }, 3, 0);
            settings.Controls.Add(first, 0, 1);
            settings.Controls.Add(second, 1, 1);
            SetupDelay(minimum, 0.30m);
            SetupDelay(maximum, 0.80m);
            settings.Controls.Add(minimum, 2, 1);
            settings.Controls.Add(maximum, 3, 1);
            root.Controls.Add(settings, 0, 3);
            useFocus.Text = "Отправлять в дочерний элемент с фокусом, если он есть";
            useFocus.Checked = true;
            useFocus.Dock = DockStyle.Fill;
            root.Controls.Add(useFocus, 0, 4);

            FlowLayoutPanel actions = new FlowLayoutPanel { Dock = DockStyle.Fill, WrapContents = false };
            SetupButton(start, "Старт", 130, delegate { StartRun(); });
            SetupButton(stop, "Стоп", 130, delegate { StopRun(); });
            start.Height = stop.Height = 40;
            start.BackColor = Color.FromArgb(224, 241, 228);
            stop.Enabled = false;
            hotkeyLabel.AutoSize = true;
            hotkeyLabel.Margin = new Padding(15, 12, 0, 0);
            actions.Controls.AddRange(new Control[] { start, stop, hotkeyLabel });
            root.Controls.Add(actions, 0, 5);

            TableLayoutPanel metrics = new TableLayoutPanel { Dock = DockStyle.Fill, ColumnCount = 2, RowCount = 1 };
            metrics.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 43));
            metrics.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 57));
            timerLabel.Dock = countLabel.Dock = DockStyle.Fill;
            timerLabel.Font = new Font("Segoe UI", 19, FontStyle.Bold);
            timerLabel.Text = "00:00:00";
            timerLabel.TextAlign = countLabel.TextAlign = ContentAlignment.MiddleLeft;
            metrics.Controls.Add(timerLabel, 0, 0);
            metrics.Controls.Add(countLabel, 1, 0);
            root.Controls.Add(metrics, 0, 6);
            status.Dock = DockStyle.Fill;
            status.ForeColor = Color.FromArgb(75, 80, 90);
            status.Text = "Кликните поле клавиши и нажмите нужную. Интервал выбирается случайно перед каждым нажатием.\nФоновые сообщения поддерживаются не всеми приложениями и играми.";
            root.Controls.Add(status, 0, 7);
            ticker.Interval = 100;
            ticker.Tick += delegate { Poll(); };
            ticker.Start();
            Shown += delegate
            {
                if (!previewMode) RefreshWindows();
                else
                {
                    available.Add(new Target { Title = "Тестовое окно 1", ProcessName = "Example", ProcessId = 1234 });
                    available.Add(new Target { Title = "Тестовое окно 2", ProcessName = "Example", ProcessId = 5678 });
                    RebuildList();
                }
            };
            FormClosing += OnClosing;
        }

        protected override bool ShowWithoutActivation { get { return previewMode; } }

        private static void SetupButton(Button button, string text, int width, EventHandler handler)
        { button.Text = text; button.Width = width; button.Height = 30; button.Click += handler; }
        private static void SetupDelay(NumericUpDown input, decimal value)
        { input.DecimalPlaces = 2; input.Minimum = 0.05m; input.Maximum = 3600; input.Increment = 0.05m; input.Value = value; input.Width = 130; }
        private static string Identity(Target target)
        { return target.Handle.ToInt64() + ":" + target.ProcessId + ":" + target.ThreadId; }

        protected override void OnHandleCreated(EventArgs e)
        {
            base.OnHandleCreated(e);
            hotkey = Native.RegisterHotKey(Handle, 0x4b38, 0x4000, (uint)Keys.F8);
            hotkeyLabel.Text = hotkey ? "F8 — глобальный стоп" : "F8 занят другой программой; используйте «Стоп»";
        }
        protected override void OnHandleDestroyed(EventArgs e)
        { if (hotkey) Native.UnregisterHotKey(Handle, 0x4b38); base.OnHandleDestroyed(e); }
        protected override void WndProc(ref Message m)
        { if (m.Msg == 0x312 && m.WParam.ToInt32() == 0x4b38) StopRun(); base.WndProc(ref m); }

        private void RefreshWindows()
        {
            available = Target.Enumerate();
            HashSet<string> valid = new HashSet<string>();
            foreach (Target target in available) valid.Add(Identity(target));
            checkedWindows.IntersectWith(valid);
            RebuildList();
        }
        private void RebuildList()
        {
            rebuilding = true;
            windows.BeginUpdate();
            try
            {
                windows.Items.Clear();
                string query = search.Text.Trim();
                foreach (Target target in available)
                {
                    if (query.Length > 0 && (target.Title + " " + target.ProcessName + " " + target.ProcessId).IndexOf(query, StringComparison.CurrentCultureIgnoreCase) < 0) continue;
                    ListViewItem row = new ListViewItem(target.Title);
                    row.SubItems.Add(target.ProcessName);
                    row.SubItems.Add(target.ProcessId.ToString());
                    row.Tag = target;
                    row.Checked = checkedWindows.Contains(Identity(target));
                    windows.Items.Add(row);
                }
            }
            finally { windows.EndUpdate(); rebuilding = false; }
            UpdateCounts();
        }
        private void SetChecks(bool value)
        { foreach (ListViewItem row in windows.Items) row.Checked = value; UpdateCounts(); }
        private void UpdateCounts()
        {
            countLabel.Text = "Выбрано окон: " + checkedWindows.Count +
                (sender == null ? "" : "    Циклов: " + sender.Rounds + "\nОтправлено нажатий: " + sender.Deliveries);
        }
        private void EnableInputs(bool enabled)
        {
            foreach (Control control in new Control[] { windows, search, refresh, selectAll, clear, first, second, minimum, maximum, useFocus, start }) control.Enabled = enabled;
            stop.Enabled = !enabled;
        }
        private void StartRun()
        {
            if (task != null) return;
            List<Target> selected = available.FindAll(delegate(Target t) { return checkedWindows.Contains(Identity(t)) && t.IsAlive(); });
            if (selected.Count == 0) { ShowValidation("Отметьте хотя бы одно существующее окно."); return; }
            if (minimum.Value > maximum.Value) { ShowValidation("Минимальная задержка должна быть не больше максимальной."); return; }
            if (first.Value == second.Value) { ShowValidation("Выберите две разные клавиши для чередования."); return; }
            int min = (int)(minimum.Value * 1000), max = (int)(maximum.Value * 1000);
            Keys a = first.Value, b = second.Value;
            bool focus = useFocus.Checked;
            cancel = new CancellationTokenSource();
            sender = new Sender();
            elapsed.Restart();
            EnableInputs(false);
            status.ForeColor = Color.FromArgb(25, 110, 55);
            status.Text = "Работает: " + KeyBox.NameOf(a) + " → " + KeyBox.NameOf(b) + " → " + KeyBox.NameOf(a) + "…  Окон: " + selected.Count + ".\nОстановить: кнопка «Стоп»" + (hotkey ? " или F8 в любом окне." : ".");
            task = Task.Factory.StartNew(delegate { return sender.Run(selected, a, b, min, max, focus, cancel.Token); }, TaskCreationOptions.LongRunning);
        }
        private void ShowValidation(string text)
        { MessageBox.Show(this, text, "Проверьте настройки", MessageBoxButtons.OK, MessageBoxIcon.Information); }
        private void StopRun()
        {
            if (cancel == null || cancel.IsCancellationRequested) return;
            cancel.Cancel();
            elapsed.Stop();
            stop.Enabled = false;
            status.Text = "Остановка и отпускание клавиш…";
        }
        private void Poll()
        {
            TimeSpan t = elapsed.Elapsed;
            timerLabel.Text = String.Format("{0:00}:{1:00}:{2:00}", (int)t.TotalHours, t.Minutes, t.Seconds);
            UpdateCounts();
            if (task == null || !task.IsCompleted) return;
            RunResult result;
            try { result = task.Result; }
            catch (AggregateException ex) { result = new RunResult { Error = true, Message = ex.GetBaseException().Message }; }
            elapsed.Stop();
            cancel.Dispose();
            cancel = null;
            task = null;
            EnableInputs(true);
            status.ForeColor = result.Error ? Color.Firebrick : Color.FromArgb(75, 80, 90);
            status.Text = result.Message;
            if (closing) Close();
        }
        private void OnClosing(object senderObject, FormClosingEventArgs e)
        {
            if (task != null)
            {
                closing = true;
                e.Cancel = true;
                StopRun();
                return;
            }
            ticker.Stop();
            ticker.Dispose();
        }
    }

    internal static class Program
    {
        [STAThread]
        private static void Main(string[] args)
        {
            if (args.Length > 0 && args[0] == "--self-test")
            {
                try { Environment.Exit(SelfTest.Run(args.Length > 1 ? args[1] : "self-test.txt")); }
                catch (Exception ex)
                {
                    File.WriteAllText(args.Length > 1 ? args[1] : "self-test.txt", ex.ToString(), Encoding.UTF8);
                    Environment.Exit(1);
                }
                return;
            }
            Native.SetProcessDPIAware();
            Application.EnableVisualStyles();
            Application.SetCompatibleTextRenderingDefault(false);
            Application.Run(new MainForm());
        }
    }
}
// EOF
