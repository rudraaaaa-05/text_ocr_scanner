# Compatibility

## Legend

| Marker | Meaning                                                       |
| ------ | ------------------------------------------------------------- |
| ✅      | Verified working on a real system of that type                |
| ❌      | Confirmed unsupported for now                                 |
| ⚠️     | Partially supported, or a known limitation / fallback applies |
| ❔      | Untested.                                                     |


## Linux distributions

Each distribution is listed with its default desktop. Desktop specific behaviour is in the next table.

| Distribution                                              | Desktop / compositor | Session         | Global hotkeys      | Background service | OCR Scanning          | Overall | Remarks                                                                        |
| --------------------------------------------------------- | -------------------- | --------------- | ------------------- | ------------------ | --------------------- | ------- | ------------------------------------------------------------------------------ |
| Ubuntu 24.04                                              | GNOME                | Wayland,<br>X11 | ❔                   | ✅                  | Wayland:⚠️<br>X11: ✅  | ⚠️      | OCR on GNOME on Wayland needs special permissions.<br>Better to use X11.       |
| Debian 12                                                 | GNOME                | Wayland, X11    | ⚠️                  | ✅                  | Wayland:⚠️<br>X11: ✅  | ⚠️      | Mostly Untested / Testing                                                      |
| Linux Mint 22                                             | Cinnamon             | X11             | ❔                   | ✅                  | ❔                     | ⚠️      | Mostly Untested / Testing                                                      |
| Arch Linux                                                | Hyprland             | Wayland         | ✅                   | ✅                  | ✅                     | ✅       | Works                                                                          |
| Arch Linux                                                | GNOME / KDE Plasma   | Wayland, X11    | Wayland:✅<br>X11: ✅ | ✅                  | Wayland: ⚠️<br>X11: ✅ | ⚠️      | OCR on GNOME / KDE on Wayland needs special permissions.<br>Better to use X11. |
| Other systemd based distributions (Fedora, openSUSE, ...) | any                  | any             | ❔                   | ❔                  | ❔                     | ❔       | Untested                                                                       |

## Desktop environments and compositors

Distribution is "any" unless stated: the code paths do not depend on the distribution.

| Distribution | Desktop / compositor                              | Session | Global hotkeys | Background service | API key storage | OCR | Remarks  |
| ------------ | ------------------------------------------------- | ------- | -------------- | ------------------ | --------------- | --- | -------- |
| any          | Hyprland                                          | Wayland | ✅              | ✅                  | ✅               | ✅   | Works    |
| any          | GNOME 48 or newer                                 | Wayland | ✅              | ✅                  | ✅               | ⚠️  |          |
| any          | GNOME                                             | X11     | ✅              | ✅                  | ✅               | ✅   | Works    |
| any          | KDE Plasma                                        | Wayland | ✅              | ✅                  | ✅               | ⚠️  |          |
| any          | KDE Plasma                                        | X11     | ✅              | ✅                  | ✅               | ✅   | Works    |
| any          | Xfce, MATE, Cinnamon, LXQt and other X11 desktops | X11     | ❔              | ❔                  | ❔               | ❔   | Untested |
| any          | sway, other wlroots compositors                   | Wayland | ❔              | ❔                  | ❔               | ❔   | Untested |


## Windows

| OS              | Desktop       | Session | Global hotkeys | Background service | API key storage | Overall | Remarks  |
| --------------- | ------------- | ------- | -------------- | ------------------ | --------------- | ------- | -------- |
| Windows 10 / 11 | Windows shell | Windows | ❔              | ❔                  | ❔               | ❔       | Untested |
