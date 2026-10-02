# Clickr

Hold a button. It clicks until you let go. And let me tell you, it clicks BIG. Tremendous clicks. The best clicks. Nobody clicks like Clickr. A beautiful, powerful, absolutely incredible clicker. Believe me, your mouse has never seen anything like it. 

Windows 10 1903+ / 11, 64-bit. Secure Boot can stay on. Simulates real mouse clicks by utilizing signed drivers making it seem like a real mouse, in theory bypasses all kinds of relevant detections except for those that deduce irregular input.

## Install

Run **`Install.bat`**. Click **Yes** on the admin prompt.

It gets Python if you don't have it, the 2 Python packages, and the driver.
Your mouse and keyboard drop out for a second. That's normal.

## Use

Run **`Clickr.bat`**. It lives in the tray.

- **L / R**: which click.
- **interval**: ms per press and per pause.
- **delta ±**: random extra 1 to *delta* ms per pause. 0 = off.
- **trigger**: click it, press any button or key. Esc cancels. Default: mouse 4 (rear).

While Clickr runs, the trigger is Clickr's only. Left click can't be the trigger.
Right click can't trigger R.

## Uninstall

1. Quit Clickr (tray icon → Quit).
2. Run **`Uninstall.bat`**. Reboot if it says so.
3. Delete the folder.

Python and the packages stay. To remove the packages:

```
python -m pip uninstall -y pystray pillow six
```

Python itself: Settings → Apps → Python → Uninstall.

Reinstalling? Reboot between `Uninstall.bat` and `Install.bat`.

## Broken?

- Read the last file in `logs\`, or `device.log`.
- Stuck or timed out: reboot, run it again.

## Credits

Driver: [usbip-win2](https://github.com/vadimgrn/usbip-win2) 0.9.7.8, BSD 2-Clause (`LICENSE-usbip-win2.txt`).
USB ID: [pid.codes](https://pid.codes) test ID `1209:0001`.
