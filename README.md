# YRadio-Qt
#### _Internet radio player with Qt (PySide6) interface_

<img width="850" height="478" alt="img" src="img.png" />

<img width="850" height="478" alt="img-black" src="img-black.png" />


### Dependencies:

```
# Debian/Ubuntu
sudo apt install mpv alsa-utils pulseaudio-utils python3 \
  python3-pyside6.qtcore python3-pyside6.qtgui python3-pyside6.qtwidgets
```

```
# Void-linux
sudo xbps-install -Su mpv alsa-utils pulseaudio-utils python3 \
  python3-pyside6 python3-pyside6-gui python3-pyside6-widgets
```

```
# Arch-linux
sudo pacman -Syu mpv alsa-utils libpulse pipewire-pulse python pyside6
```

### How to use YRadio:

- **Select station:** click on it twice or highlight and click Play.
- **Find a station:** click Search Stations, select a country and/or enter a station name or tag. Select a result and click Add Selected, to save a station to the current playlist.
- **Manage playlists:** select a playlist from the list above. Buttons allow you to add or remove a station, create a playlist and copy a station to another playlist.
- **Select player:** right click on the YRadio icon in the system tray, open Select player (mpv or mplayer) and select the installed player. The selection is applied the next time the station is started.
- **Pause, volume and stop:** use the buttons in the program window or the corresponding menu items of the tray icon.
- **Record:** start the station and press Start Recording. Required for registration mpv and file yradio_ripper.lua next to the program. When you first start recording, select a folder for the files.

### Setting up Qt6 applications using the ( Kvantum Manager ) and ( Qt6 Configuration Tool ) for Void Linux ( Xfce )

_XFCE primarily configures GTK applications, while Plasma has native Qt integration and the Breeze theme. For Qt applications outside of Plasma, you need to select a style and palette separately._

```
sudo xbps install -Su kvantum qt6ct breeze-icons
```

- Run the Qt6 Configuration Tool

- In QT6 Settings, select Appearance > Kvantum Style

- In **/etc/environment** we add `QT_QPA_PLATFORMTHEME=qt6ct`

- Log out and log back in

- **Check in the terminal:** `echo $QT_QPA_PLATFORMTHEME` if Qt6 is connected, you will see the output **qt6ct**

<br>

<img width="850" height="478" alt="snapedit_1795854253921" src="https://github.com/user-attachments/assets/d6c862e9-8269-4d34-b461-48b712dc5a89" />


