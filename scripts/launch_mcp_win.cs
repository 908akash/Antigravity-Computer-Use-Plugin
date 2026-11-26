using System;
using System.Diagnostics;
using System.IO;
using System.Reflection;
using System.Text;

internal static class LaunchMcpWin
{
    private static string ResolveCmdLauncher()
    {
        string[] dirs = new string[]
        {
            AppDomain.CurrentDomain.BaseDirectory,
            Path.GetDirectoryName(Assembly.GetExecutingAssembly().Location)
        };
        for (int i = 0; i < dirs.Length; i++)
        {
            string dir = dirs[i];
            if (!string.IsNullOrEmpty(dir))
            {
                string cand = Path.Combine(dir, "launch_mcp.cmd");
                if (File.Exists(cand))
                {
                    return Path.GetFullPath(cand);
                }
            }
        }
        return null;
    }

    private static string ResolveCmdExe()
    {
        string comSpec = Environment.GetEnvironmentVariable("ComSpec");
        if (!string.IsNullOrEmpty(comSpec) && File.Exists(comSpec))
        {
            return comSpec;
        }
        string sysCmd = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.System), "cmd.exe");
        if (File.Exists(sysCmd))
        {
            return sysCmd;
        }
        return "cmd.exe";
    }

    private static string QuoteArg(string arg)
    {
        if (arg == null || arg.Length == 0)
        {
            return "\"\"";
        }
        bool needsQuotes = false;
        for (int i = 0; i < arg.Length; i++)
        {
            char c = arg[i];
            if (char.IsWhiteSpace(c) || c == '"' || c == '&' || c == '|' || c == '<' || c == '>' || c == '^' || c == '(' || c == ')' || c == '!')
            {
                needsQuotes = true;
                break;
            }
        }
        if (!needsQuotes)
        {
            return arg;
        }
        StringBuilder sb = new StringBuilder();
        sb.Append('"');
        int backslashes = 0;
        for (int i = 0; i < arg.Length; i++)
        {
            char c = arg[i];
            if (c == '\\')
            {
                backslashes++;
            }
            else if (c == '"')
            {
                sb.Append('\\', backslashes * 2 + 1);
                sb.Append('"');
                backslashes = 0;
            }
            else
            {
                if (backslashes > 0)
                {
                    sb.Append('\\', backslashes);
                    backslashes = 0;
                }
                sb.Append(c);
            }
        }
        if (backslashes > 0)
        {
            sb.Append('\\', backslashes * 2);
        }
        sb.Append('"');
        return sb.ToString();
    }

    public static int Main(string[] args)
    {
        string cmdPath = ResolveCmdLauncher();
        if (string.IsNullOrEmpty(cmdPath))
        {
            Console.Error.WriteLine("[computer-use] Missing launcher script: launch_mcp.cmd");
            return 1;
        }

        StringBuilder inner = new StringBuilder();
        inner.Append('"');
        inner.Append(cmdPath);
        inner.Append('"');
        if (args != null)
        {
            for (int i = 0; i < args.Length; i++)
            {
                inner.Append(' ');
                inner.Append(QuoteArg(args[i]));
            }
        }

        Console.CancelKeyPress += delegate(object sender, ConsoleCancelEventArgs e)
        {
            e.Cancel = true;
        };

        ProcessStartInfo psi = new ProcessStartInfo();
        psi.FileName = ResolveCmdExe();
        psi.Arguments = "/d /s /c \"" + inner.ToString() + "\"";
        psi.UseShellExecute = false;

        try
        {
            using (Process proc = Process.Start(psi))
            {
                if (proc == null)
                {
                    Console.Error.WriteLine("[computer-use] Failed to launch cmd.exe.");
                    return 1;
                }
                proc.WaitForExit();
                return proc.ExitCode;
            }
        }
        catch (Exception ex)
        {
            Console.Error.WriteLine("[computer-use] Failed to execute launcher: " + ex.Message);
            return 1;
        }
    }
}
