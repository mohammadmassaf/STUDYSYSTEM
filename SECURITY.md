# Security

Report a security problem privately: on GitHub, open the repo's **Security** tab and choose **Report a
vulnerability**. Please do not open a public issue for it.

Only the newest version gets fixes.

## What to know

- The dashboard answers on 127.0.0.1 only, and only a browser that the study system opened. The secret
  is `dashboard.key` in the data folder: a program that runs in your own account can read it.
- The study system reads the files that your AI sends it. Add only files that you trust, and read what
  your AI asks before you allow a tool call.
