using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using System.Windows.Forms;

namespace BackgroundKeys
{
    // Uses only two private, invisible message windows. Never targets user applications.
    internal static class SelfTest
    {
        private sealed class RecordedKey
        {
            internal Keys Key;
            internal bool Up;
            internal long Flags, At;
        }
        private sealed class Probe : NativeWindow, IDisposable
        {
            internal readonly List<RecordedKey> Events = new List<RecordedKey>();
            internal Probe()
            {
                CreateHandle(new CreateParams { Caption = "BackgroundKeys self-test", Parent = new IntPtr(-3) });
            }
            protected override void WndProc(ref Message message)
            {
                if (message.Msg == Native.KeyDown || message.Msg == Native.KeyUp)
                    Events.Add(new RecordedKey { Key = (Keys)message.WParam.ToInt32(), Up = message.Msg == Native.KeyUp,
                        Flags = message.LParam.ToInt64() & 0xffffffffL, At = Stopwatch.GetTimestamp() });
                base.WndProc(ref message);
            }
            internal Target Target()
            {
                uint pid;
                uint thread = Native.GetWindowThreadProcessId(Handle, out pid);
                return new Target { Handle = Handle, ProcessId = pid, ThreadId = thread, Title = "Private test window" };
            }
            public void Dispose() { DestroyHandle(); }
        }
        private static void Check(bool condition, string message)
        { if (!condition) throw new InvalidOperationException(message); }
        private static double Milliseconds(long ticks) { return ticks * 1000.0 / Stopwatch.Frequency; }
        private static void PumpUntil(Func<bool> done)
        {
            Stopwatch timeout = Stopwatch.StartNew();
            while (!done())
            {
                Application.DoEvents();
                if (timeout.ElapsedMilliseconds > 6000) throw new TimeoutException("Self-test exceeded 6 seconds.");
                Thread.Sleep(2);
            }
            Application.DoEvents();
        }
        internal static int Run(string report)
        {
            List<string> lines = new List<string>();
            IntPtr foreground = Native.GetForegroundWindow();
            using (Probe left = new Probe())
            using (Probe right = new Probe())
            using (CancellationTokenSource stop = new CancellationTokenSource())
            {
                List<Target> targets = new List<Target> { left.Target(), right.Target() };
                Sender sender = new Sender();
                Stopwatch runTime = Stopwatch.StartNew();
                Task<RunResult> run = Task.Factory.StartNew(delegate
                { return sender.Run(targets, Keys.D3, Keys.D4, 80, 130, false, stop.Token); });
                try
                {
                    PumpUntil(delegate { return left.Events.Count >= 12 && right.Events.Count >= 12; });
                    stop.Cancel();
                    PumpUntil(delegate { return run.IsCompleted; });
                    Check(!run.Result.Error, run.Result.Message);
                    Check(sender.Rounds >= 6 && sender.Deliveries == sender.Rounds * 2, "Broadcast counters do not match.");
                    Check(left.Events.Count == right.Events.Count, "Windows received different event counts.");
                    for (int i = 0; i < left.Events.Count; i++)
                    {
                        RecordedKey a = left.Events[i], b = right.Events[i];
                        Check(a.Key == b.Key && a.Up == b.Up, "Broadcast order differs.");
                        Check(a.Up == (i % 2 == 1), "Keydown/keyup are unbalanced.");
                        Keys expected = (i / 2) % 2 == 0 ? Keys.D3 : Keys.D4;
                        Check(a.Key == expected, "Keys do not alternate.");
                        Check((a.Flags & 0xffff) == 1, "Invalid repeat count.");
                        Check(((a.Flags >> 16) & 0xff) != 0, "Scan code missing.");
                        Check((a.Flags & 0xc0000000L) == (a.Up ? 0xc0000000L : 0), "Invalid transition flags.");
                    }
                    // Receiver timestamps can bunch together when Windows delays its queue.
                    // Measure total wall time instead of mistaking queue jitter for send timing.
                    Check(runTime.ElapsedMilliseconds >= 400 && runTime.ElapsedMilliseconds < 3000,
                        "Unexpected duration for six rounds: " + runTime.ElapsedMilliseconds);
                    Check(Native.GetForegroundWindow() == foreground, "Foreground window was changed.");
                    lines.Add("PASS: alternating 3/4 in two background windows; matched broadcasts; keyup pairing and flags; random intervals; unchanged foreground.");
                }
                finally { stop.Cancel(); run.Wait(2000); }
            }
            using (Probe probe = new Probe())
            using (CancellationTokenSource stop = new CancellationTokenSource())
            {
                Sender sender = new Sender();
                List<Target> targets = new List<Target> { probe.Target() };
                Task<RunResult> run = Task.Factory.StartNew(delegate
                { return sender.Run(targets, Keys.A, Keys.B, 120000, 120000, true, stop.Token); });
                try
                {
                    PumpUntil(delegate { return probe.Events.Count > 0; });
                    Stopwatch reaction = Stopwatch.StartNew();
                    stop.Cancel();
                    PumpUntil(delegate { return run.IsCompleted; });
                    Check(reaction.ElapsedMilliseconds < 500, "Stop was not prompt.");
                    Check(probe.Events.Count == 2 && !probe.Events[0].Up && probe.Events[1].Up, "Stop failed to release the held key.");
                    Check(sender.Rounds == 1 && !run.Result.Error, "Unexpected activity after stop.");
                    lines.Add("PASS: cancellation during key hold releases the key and interrupts a 120-second delay in under 500 ms.");
                }
                finally { stop.Cancel(); run.Wait(2000); }
            }
            using (Probe probe = new Probe())
            {
                Target closed = probe.Target();
                probe.Dispose();
                Sender sender = new Sender();
                RunResult result = sender.Run(new List<Target> { closed }, Keys.D3, Keys.D4, 80, 130, false, CancellationToken.None);
                Check(!result.Error && sender.Rounds == 0, "Closed target was not removed.");
                lines.Add("PASS: closing all target windows stops the sender without further input.");
            }
            using (MainForm form = new MainForm(true))
            {
                Check(form.ClientSize.Width >= 820 && form.Controls.Count > 0, "Main form construction failed.");
                form.ShowInTaskbar = false;
                form.StartPosition = FormStartPosition.Manual;
                form.Location = new Point(-32000, -32000);
                form.Show();
                Application.DoEvents();
                using (Bitmap preview = new Bitmap(form.Width, form.Height))
                {
                    form.DrawToBitmap(preview, new Rectangle(Point.Empty, form.Size));
                    preview.Save(Path.Combine(Path.GetDirectoryName(Path.GetFullPath(report)), "preview.png"));
                }
                form.Close();
                Check(Native.GetForegroundWindow() == foreground, "UI preview changed the foreground window.");
                lines.Add("PASS: application form, hotkey registration and settings construct and render successfully.");
            }
            lines.Add("All checks passed. " + DateTimeOffset.Now.ToString("O"));
            File.WriteAllLines(report, lines.ToArray(), Encoding.UTF8);
            return 0;
        }
    }
}
// EOF
