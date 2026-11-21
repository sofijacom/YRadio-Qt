#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from PySide6.QtCore import QObject, QProcess, QRunnable, QThreadPool, Qt, Signal
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QSlider,
    QStyle,
    QSystemTrayIcon,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

APP_DIR = Path(__file__).resolve().parent
ICON_DIR = APP_DIR / "icons"
HOME = Path.home()

DATA_DIR = HOME / ".yradio"
PLAYLIST_DIR = DATA_DIR / "playlist"
TMP_DIR = DATA_DIR / "tmp"

NOW_URL = TMP_DIR / "nowurl"
NOW_PLAY = TMP_DIR / "nowplay"
PLAYER_FILE = DATA_DIR / "defplayer"
RIP_CONF = HOME / ".config" / "streamrip.conf"

RB_HOSTS = ("de1", "de2", "all")
# Small pages: some networks stall responses after ~16 KB, so each page
# must stay well below that. Pages are fetched in parallel to compensate.
RB_LIMIT = 8
RB_MAX_STATIONS = 10000
RB_RETRIES = 3
RB_TIMEOUT = 15
RB_WORKERS = 10

def app_icon(icon_file, theme_name, standard_name):
    """Сначала ищет PNG в ./icons, затем системную иконку и значок Qt."""
    icon_path = ICON_DIR / icon_file
    if icon_path.is_file():
        return QIcon(str(icon_path))

    icon = QIcon.fromTheme(theme_name)
    if not icon.isNull():
        return icon

    standard = getattr(
        QStyle.StandardPixmap,
        standard_name,
        QStyle.StandardPixmap.SP_FileIcon,
    )
    return QApplication.style().standardIcon(standard)

def run_command(args, timeout=3):
    try:
        result = subprocess.run(
            args,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
        return result.returncode, result.stdout.strip(), result.stderr.strip()
    except (OSError, subprocess.TimeoutExpired):
        return 1, "", ""

class WorkerSignals(QObject):
    finished = Signal(object, object, str)

class CountryWorker(QRunnable):
    def __init__(self):
        super().__init__()
        self.signals = WorkerSignals()

    def run(self):
        countries = []
        errors = []

        for host in RB_HOSTS:
            url = f"https://{host}.api.radio-browser.info/json/countries"
            request = Request(
                url,
                headers={"User-Agent": "YRadio-Qt/1.0"},
            )

            try:
                with urlopen(request, timeout=RB_TIMEOUT) as response:
                    data = response.read()

                result = json.loads(data.decode("utf-8"))
                if not isinstance(result, list):
                    raise ValueError("Unexpected response from Radio Browser")

                if result:
                    countries = result
                    break

            except Exception as exc:
                errors.append(f"{host}: {exc}")

        self.signals.finished.emit(self, countries, "\n".join(errors))

class SearchWorker(QRunnable):
    def __init__(self, query, country):
        super().__init__()
        self.query = query.strip()
        self.country = country or {}
        self.signals = WorkerSignals()

    @staticmethod
    def _fetch_page(host, path, filters, offset):
        params = dict(filters)
        params.update({
            "order": "name",
            "limit": str(RB_LIMIT),
            "offset": str(offset),
            "hidebroken": "true",
        })

        url = (
            f"https://{host}.api.radio-browser.info"
            f"{path}?{urlencode(params)}"
        )
        request = Request(
            url,
            headers={
                "User-Agent": "YRadio-Qt/1.0",
                "Accept": "application/json",
            },
        )

        last_error = None

        for attempt in range(RB_RETRIES):
            try:
                with urlopen(request, timeout=RB_TIMEOUT) as response:
                    data = response.read()

                if not data.strip():
                    raise ValueError("The server returned an empty response")

                result = json.loads(data.decode("utf-8"))
                if not isinstance(result, list):
                    raise ValueError("Unexpected response from Radio Browser")

                return result

            except (
                HTTPError,
                URLError,
                TimeoutError,
                OSError,
                UnicodeDecodeError,
                json.JSONDecodeError,
                ValueError,
            ) as exc:
                last_error = exc
                if attempt + 1 < RB_RETRIES:
                    time.sleep(1)

        raise RuntimeError(str(last_error or "Could not fetch results"))

    def _fetch_all(self, host, path, filters):
        stations = []
        offset = 0

        with ThreadPoolExecutor(max_workers=RB_WORKERS) as pool:
            while offset < RB_MAX_STATIONS:
                offsets = [
                    offset + i * RB_LIMIT for i in range(RB_WORKERS)
                ]
                pages = list(
                    pool.map(
                        lambda o: self._fetch_page(
                            host, path, filters, o
                        ),
                        offsets,
                    )
                )

                finished = False
                for page in pages:
                    stations.extend(page)
                    if len(page) < RB_LIMIT:
                        finished = True
                        break

                if finished:
                    break

                offset += RB_WORKERS * RB_LIMIT

        return stations

    @staticmethod
    def _station_key(station):
        return (
            station.get("stationuuid")
            or station.get("url_resolved")
            or station.get("url")
        )

    def _get_country_stations(self, host, country):
        code = str(country.get("code") or "").strip()
        name = str(country.get("name") or "").strip()

        if code:
            stations = self._fetch_all(
                host,
                "/json/stations/search",
                {"countrycode": code},
            )
            if stations:
                return stations

        encoded = quote(name, safe="")
        stations = self._fetch_all(
            host,
            f"/json/stations/bycountryexact/{encoded}",
            {},
        )
        if not stations:
            stations = self._fetch_all(
                host,
                f"/json/stations/bycountry/{encoded}",
                {},
            )

        return stations

    def run(self):
        country_name = str(self.country.get("name") or "").strip()
        results = {}
        errors = []

        for host in RB_HOSTS:
            try:
                if country_name:
                    stations = self._get_country_stations(host, self.country)

                    # Apply the optional query to the selected country's
                    # stations, rather than searching all countries.
                    if self.query:
                        query = self.query.casefold()
                        stations = [
                            station
                            for station in stations
                            if query in str(
                                station.get("name") or ""
                            ).casefold()
                            or query in str(
                                station.get("tags") or ""
                            ).casefold()
                        ]

                elif self.query:
                    stations = []
                    for field in ("name", "tag"):
                        stations.extend(
                            self._fetch_all(
                                host,
                                "/json/stations/search",
                                {field: self.query},
                            )
                        )
                else:
                    stations = []

                for station in stations:
                    key = self._station_key(station)
                    if key:
                        results[key] = station

                # Stop at the first server that returned something.
                if results:
                    break

            except Exception as exc:
                errors.append(f"{host}: {exc}")

        self.signals.finished.emit(
            self,
            list(results.values()),
            "\n".join(errors),
        )

class SearchDialog(QDialog):
    def __init__(self, parent):
        super().__init__(parent)
        self.parent_window = parent
        self.setWindowTitle("Search Radio Stations")
        self.setWindowIcon(
            app_icon(
                "internetradio.png",
                "audio-radio",
                "SP_MediaPlay",
            )
        )
        # 1050, 650
        self.resize(850, 600)

        self.query_edit = QLineEdit()
        self.query_edit.setPlaceholderText(
            "Station name or tag; leave empty to list stations in a country"
        )

        self.country_combo = QComboBox()
        self.country_combo.addItem("All Countries", None)
        self.country_combo.setMinimumWidth(420)

        self.search_button = QPushButton("Search")
        self.add_button = QPushButton("Add Selected")
        self.save_all_button = QPushButton("Save All to New Playlist")
        self.close_button = QPushButton("Close")

        self.search_button.setIcon(
            app_icon(
                "list-add.png",
                "system-search",
                "SP_FileDialogContentsView",
            )
        )
        self.add_button.setIcon(
            app_icon(
                "gtk-new1.png",
                "list-add",
                "SP_FileDialogNewFolder",
            )
        )
        self.save_all_button.setIcon(
            app_icon(
                "save.png",
                "document-save",
                "SP_DialogSaveButton",
            )
        )
        self.close_button.setIcon(
            app_icon(
                "quit.png",
                "window-close",
                "SP_DialogCloseButton",
            )
        )

        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["Station", "Country", "Tags"])
        self.table.setSelectionBehavior(
            QTableWidget.SelectionBehavior.SelectRows
        )
        self.table.setSelectionMode(
            QTableWidget.SelectionMode.SingleSelection
        )
        self.table.setEditTriggers(
            QTableWidget.EditTrigger.NoEditTriggers
        )
        self.table.setAlternatingRowColors(True)
        self.table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.ResizeToContents
        )
        self.table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeMode.ResizeToContents
        )
        self.table.horizontalHeader().setSectionResizeMode(
            2, QHeaderView.ResizeMode.Stretch
        )

        self.message = QLabel("Loading country list...")
        self.message.setWordWrap(True)

        form = QFormLayout()
        form.addRow("Station or tag:", self.query_edit)
        form.addRow("Country:", self.country_combo)

        buttons = QHBoxLayout()
        buttons.addWidget(self.search_button)
        buttons.addStretch(1)
        buttons.addWidget(self.add_button)
        buttons.addWidget(self.save_all_button)
        buttons.addWidget(self.close_button)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(self.table, 1)
        layout.addWidget(self.message)
        layout.addLayout(buttons)

        self.search_button.clicked.connect(self.start_search)
        self.query_edit.returnPressed.connect(self.start_search)
        self.add_button.clicked.connect(
            lambda: self.parent_window.add_search_result(self)
        )
        self.save_all_button.clicked.connect(
            lambda: self.parent_window.save_search_results(self)
        )
        self.close_button.clicked.connect(self.close)

    def set_countries(self, countries):
        self.country_combo.clear()
        self.country_combo.addItem("All Countries", None)

        for entry in countries:
            name = str(entry.get("name") or "").strip()
            if not name:
                continue

            try:
                count = int(entry.get("stationcount", 0))
            except (TypeError, ValueError):
                count = 0

            self.country_combo.addItem(
                f"{name} ({count:,} stations)",
                {
                    "name": name,
                    "code": str(entry.get("iso_3166_1") or "").upper(),
                },
            )

        self.message.setText(
            "Choose a country or enter a station name or tag."
        )

    def start_search(self):
        query = self.query_edit.text().strip()
        country = self.country_combo.currentData() or {}

        if not query and not country:
            QMessageBox.information(
                self,
                "Search",
                "Enter a station name or tag, or choose a country.",
            )
            return

        self.search_button.setEnabled(False)
        self.save_all_button.setEnabled(False)
        self.search_button.setText("Searching...")
        self.message.setText("Searching Radio Browser...")
        self.table.setRowCount(0)

        worker = SearchWorker(query, country)
        self.parent_window.workers[worker] = self
        worker.signals.finished.connect(
            self.parent_window.search_finished
        )
        QThreadPool.globalInstance().start(worker)

    def set_results(self, stations):
        self.table.setRowCount(0)

        for station in stations:
            row = self.table.rowCount()
            self.table.insertRow(row)

            name = str(station.get("name") or "(unnamed station)")
            country = str(station.get("country") or "")
            tags = str(station.get("tags") or "")

            item = QTableWidgetItem(name)
            item.setData(Qt.ItemDataRole.UserRole, station)
            self.table.setItem(row, 0, item)
            self.table.setItem(row, 1, QTableWidgetItem(country))
            self.table.setItem(row, 2, QTableWidgetItem(tags))

        self.message.setText(f"Stations found: {len(stations)}.")

    def selected_station(self):
        row = self.table.currentRow()
        if row < 0:
            return None

        item = self.table.item(row, 0)
        return item.data(Qt.ItemDataRole.UserRole) if item else None

class VolumeDialog(QDialog):
    def __init__(self, parent, value, callback):
        super().__init__(
            parent,
            Qt.WindowType.Tool
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint,
        )
        self.callback = callback
        self.setFixedSize(50, 200)

        self.label = QLabel(f"{value}%")
        self.label.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.slider = QSlider(Qt.Orientation.Vertical)
        self.slider.setRange(0, 100)
        self.slider.setValue(value)
        self.slider.valueChanged.connect(self.changed)

        layout = QVBoxLayout(self)
        layout.addWidget(self.label)
        layout.addWidget(self.slider, 1, Qt.AlignmentFlag.AlignHCenter)

    def changed(self, value):
        self.label.setText(f"{value}%")
        self.callback(value)

class YRadioWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("YRadio")
        self.setWindowIcon(
            app_icon(
                "internet_radio_48.png",
                "audio-radio",
                "SP_MediaPlay",
            )
        )
        self.resize(850, 600)

        DATA_DIR.mkdir(parents=True, exist_ok=True)
        PLAYLIST_DIR.mkdir(parents=True, exist_ok=True)
        TMP_DIR.mkdir(parents=True, exist_ok=True)

        self._create_default_playlist()
        self.current_playlist = "Favorites"
        self.current_url = ""
        self.current_station = ""
        self.paused = False
        self.volume_dialog = None
        self.workers = {}

        self.player_name = self._find_player()

        self.player = QProcess(self)
        self.player.setProcessChannelMode(
            QProcess.ProcessChannelMode.MergedChannels
        )
        self.player.readyReadStandardOutput.connect(self.read_player_output)
        self.player.finished.connect(self.player_finished)

        self.ripper = QProcess(self)
        self.ripper.setProcessChannelMode(
            QProcess.ProcessChannelMode.MergedChannels
        )
        self.ripper.finished.connect(self.ripper_finished)

        self.pool = QThreadPool.globalInstance()
        self._build_ui()
        self._build_tray()
        self.refresh_playlists()
        self.load_playlist()

    def _create_default_playlist(self):
        if list(PLAYLIST_DIR.glob("playlist.*")):
            return

        packaged = APP_DIR / "playlist"
        if packaged.is_dir():
            for source in packaged.glob("playlist.*"):
                try:
                    shutil.copy2(source, PLAYLIST_DIR / source.name)
                except OSError:
                    pass

        if not list(PLAYLIST_DIR.glob("playlist.*")):
            (PLAYLIST_DIR / "playlist.Favorites").write_text(
                "https://stream.radioparadise.com/mp3-192"
                "|Radio Paradise|Eclectic\n",
                encoding="utf-8",
            )

    def _find_player(self):
        try:
            selected = PLAYER_FILE.read_text(
                encoding="utf-8"
            ).strip().lower()
        except OSError:
            selected = ""

        if selected in {"mpv", "mplayer"} and shutil.which(selected):
            return selected
        if shutil.which("mpv"):
            return "mpv"
        if shutil.which("mplayer"):
            return "mplayer"
        return ""

    def _build_ui(self):
        self.playlist_combo = QComboBox()
        self.playlist_combo.currentTextChanged.connect(
            self.playlist_changed
        )

        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(
            ["Station", "Description", "URL"]
        )
        self.table.setColumnHidden(2, True)
        self.table.setSelectionBehavior(
            QTableWidget.SelectionBehavior.SelectRows
        )
        self.table.setSelectionMode(
            QTableWidget.SelectionMode.SingleSelection
        )
        self.table.setEditTriggers(
            QTableWidget.EditTrigger.NoEditTriggers
        )
        self.table.setAlternatingRowColors(True)
        self.table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.ResizeToContents
        )
        self.table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeMode.Stretch
        )
        self.table.cellDoubleClicked.connect(
            lambda *_: self.play_selected()
        )

        self.status = QLabel("Ready")
        self.status.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )

        self.search_button = QPushButton("Search Stations")
        self.play_button = QPushButton("Play")
        self.stop_button = QPushButton("Stop")
        self.pause_button = QPushButton("Pause")
        self.volume_button = QPushButton("Volume")
        self.add_button = QPushButton("Add Station")
        self.remove_button = QPushButton("Remove")
        self.new_playlist_button = QPushButton("New Playlist")
        self.move_button = QPushButton("Copy to Playlist")
        self.rip_button = QPushButton("Start Recording")

        button_icons = [
            (
                self.search_button,
                "list-add.png",
                "system-search",
                "SP_FileDialogContentsView",
            ),
            (
                self.play_button,
                "pause_play.png",
                "media-playback-start",
                "SP_MediaPlay",
            ),
            (
                self.stop_button,
                "stop.png",
                "media-playback-stop",
                "SP_MediaStop",
            ),
            (
                self.pause_button,
                "pause_play.png",
                "media-playback-pause",
                "SP_MediaPause",
            ),
            (
                self.volume_button,
                "volume.png",
                "audio-volume-high",
                "SP_MediaVolume",
            ),
            (
                self.add_button,
                "gtk-new1.png",
                "list-add",
                "SP_FileDialogNewFolder",
            ),
            (
                self.remove_button,
                "gtk-remove.png",
                "list-remove",
                "SP_TrashIcon",
            ),
            (
                self.new_playlist_button,
                "gtk-new1.png",
                "document-new",
                "SP_FileDialogNewFolder",
            ),
            (
                self.move_button,
                "list-add.png",
                "go-next",
                "SP_ArrowRight",
            ),
            (
                self.rip_button,
                "record.png",
                "media-record",
                "SP_DialogSaveButton",
            ),
        ]

        for button, icon_file, theme_name, standard_name in button_icons:
            button.setIcon(
                app_icon(icon_file, theme_name, standard_name)
            )

        top = QHBoxLayout()
        top.addWidget(QLabel("Playlist:"))
        top.addWidget(self.playlist_combo, 1)
        top.addWidget(self.search_button)

        playback = QHBoxLayout()
        for button in (
            self.play_button,
            self.stop_button,
            self.pause_button,
            self.volume_button,
            self.rip_button,
        ):
            playback.addWidget(button)

        playlist_buttons = QHBoxLayout()
        for button in (
            self.add_button,
            self.remove_button,
            self.move_button,
            self.new_playlist_button,
        ):
            playlist_buttons.addWidget(button)

        central = QWidget()
        layout = QVBoxLayout(central)
        layout.addLayout(top)
        layout.addWidget(self.table, 1)
        layout.addLayout(playback)
        layout.addLayout(playlist_buttons)
        layout.addWidget(self.status)
        self.setCentralWidget(central)

        self.search_button.clicked.connect(self.open_search)
        self.play_button.clicked.connect(self.play_selected)
        self.stop_button.clicked.connect(self.stop_playback)
        self.pause_button.clicked.connect(self.toggle_pause)
        self.volume_button.clicked.connect(self.show_volume)
        self.add_button.clicked.connect(self.add_custom_station)
        self.remove_button.clicked.connect(self.remove_selected)
        self.new_playlist_button.clicked.connect(self.create_playlist)
        self.move_button.clicked.connect(self.copy_selected_to_playlist)
        self.rip_button.clicked.connect(self.toggle_ripping)

    def _build_tray(self):
        self.tray = QSystemTrayIcon(self)
        self.tray.setIcon(
            app_icon(
                "internetradio.png",
                "audio-radio",
                "SP_MediaPlay",
            )
        )
        self.tray.setToolTip("YRadio")

        menu = QMenu(self)
        menu.addAction(
            app_icon("internetradio.png", "go-home", "SP_DesktopIcon"),
            "Show YRadio",
            self.show_and_raise,
        )
        menu.addAction(
            app_icon("stop.png", "media-playback-stop", "SP_MediaStop"),
            "Stop",
            self.stop_playback,
        )
        menu.addAction(
            app_icon(
                "pause_play.png",
                "media-playback-pause",
                "SP_MediaPause",
            ),
            "Pause / Resume",
            self.toggle_pause,
        )
        menu.addAction(
            app_icon(
                "volume.png",
                "audio-volume-high",
                "SP_MediaVolume",
            ),
            "Volume",
            self.show_volume,
        )
        menu.addAction(
            app_icon("record.png", "media-record", "SP_DialogSaveButton"),
            "Start / Stop Recording",
            self.toggle_ripping,
        )
        menu.addSeparator()
        menu.addAction(
            app_icon("quit.png", "application-exit", "SP_DialogCloseButton"),
            "Exit",
            self.close,
        )

        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self.tray_activated)

        if QSystemTrayIcon.isSystemTrayAvailable():
            self.tray.show()

    def tray_activated(self, reason):
        if reason in (
            QSystemTrayIcon.ActivationReason.Trigger,
            QSystemTrayIcon.ActivationReason.DoubleClick,
        ):
            self.show_and_raise()

    def show_and_raise(self):
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def playlist_files(self):
        return sorted(
            PLAYLIST_DIR.glob("playlist.*"),
            key=lambda path: (
                path.name != "playlist.Favorites",
                path.name.lower(),
            ),
        )

    def refresh_playlists(self, select=None):
        previous = (
            select
            or self.playlist_combo.currentText()
            or "Favorites"
        )

        self.playlist_combo.blockSignals(True)
        self.playlist_combo.clear()

        for path in self.playlist_files():
            self.playlist_combo.addItem(path.name[len("playlist."):])

        index = self.playlist_combo.findText(previous)
        if index >= 0:
            self.playlist_combo.setCurrentIndex(index)

        self.playlist_combo.blockSignals(False)
        self.current_playlist = (
            self.playlist_combo.currentText() or "Favorites"
        )

    def playlist_path(self, name=None):
        return PLAYLIST_DIR / f"playlist.{name or self.current_playlist}"

    def playlist_changed(self, name):
        if name:
            self.current_playlist = name
            self.load_playlist()

    def load_playlist(self):
        self.table.setRowCount(0)

        try:
            lines = self.playlist_path().read_text(
                encoding="utf-8",
                errors="replace",
            ).splitlines()
        except OSError:
            lines = []

        for line in lines:
            fields = line.split("|", 2)
            if len(fields) < 2:
                continue

            url = fields[0].strip()
            name = fields[1].strip() or url
            description = fields[2].strip() if len(fields) > 2 else ""
            if not url:
                continue

            row = self.table.rowCount()
            self.table.insertRow(row)

            item = QTableWidgetItem(name)
            item.setData(
                Qt.ItemDataRole.UserRole,
                (url, name, description),
            )
            self.table.setItem(row, 0, item)
            self.table.setItem(row, 1, QTableWidgetItem(description))
            self.table.setItem(row, 2, QTableWidgetItem(url))

        self.status.setText(
            f"{self.current_playlist}: {self.table.rowCount()} stations"
        )

    def selected_playlist_station(self):
        row = self.table.currentRow()
        if row < 0:
            return None

        item = self.table.item(row, 0)
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    def play_selected(self):
        station = self.selected_playlist_station()
        if not station:
            QMessageBox.information(
                self,
                "YRadio",
                "Select a station first.",
            )
            return

        self.play_station(station[0], station[1])

    def play_station(self, url, name):
        if not self.player_name:
            QMessageBox.critical(
                self,
                "Player Not Found",
                "Install mpv or mplayer.",
            )
            return

        self.stop_playback()
        self.current_url = url
        self.current_station = name
        self.paused = False

        NOW_URL.write_text(url + "\n", encoding="utf-8")
        NOW_PLAY.write_text(name + "\n", encoding="utf-8")

        if self.player_name == "mpv":
            program = shutil.which("mpv")
            args = [
                "--no-video",
                "--input-terminal=no",
                "--display-tags=icy-name,icy-title,icy-genre,icy-br",
                url,
            ]
        else:
            program = shutil.which("mplayer")
            args = ["-quiet", "-cache", "320", "-cache-min", "8", url]

        self.player.start(program, args)
        self.status.setText(f"Starting: {name}")

    def stop_playback(self):
        if self.player.state() != QProcess.ProcessState.NotRunning:
            self.player.terminate()
            if not self.player.waitForFinished(1200):
                self.player.kill()
                self.player.waitForFinished(1000)

        self.paused = False
        self.pause_button.setText("Pause")

    def toggle_pause(self):
        if self.player.state() != QProcess.ProcessState.Running:
            return

        try:
            pid = int(self.player.processId())
            os.kill(pid, signal.SIGCONT if self.paused else signal.SIGSTOP)
            self.paused = not self.paused
            self.pause_button.setText(
                "Resume" if self.paused else "Pause"
            )
        except (OSError, ProcessLookupError, ValueError):
            pass

    def read_player_output(self):
        text = bytes(self.player.readAllStandardOutput()).decode(
            "utf-8",
            errors="replace",
        )

        for line in text.splitlines():
            match = re.search(r"icy-title[:=]\s*(.*)", line, re.IGNORECASE)
            if match and match.group(1).strip():
                title = f"{self.current_station} — {match.group(1).strip()}"
                self.status.setText(f"Playing: {title}")
                self.tray.setToolTip(title[:120])

    def player_finished(self, _code, _status):
        self.paused = False
        self.pause_button.setText("Pause")

    def open_search(self):
        self.search_dialog = SearchDialog(self)
        self.search_dialog.show()
        self.search_dialog.raise_()
        self.search_dialog.activateWindow()

        worker = CountryWorker()
        self.workers[worker] = self.search_dialog
        worker.signals.finished.connect(self.countries_finished)
        self.pool.start(worker)

    def countries_finished(self, worker, countries, error):
        dialog = self.workers.pop(worker, None)
        if dialog is None:
            return

        if countries:
            dialog.set_countries(countries)
        else:
            dialog.message.setText(
                "Could not load countries. "
                f"Error: {error or 'empty server response'}"
            )

    def search_finished(self, worker, stations, error):
        dialog = self.workers.pop(worker, None)
        if dialog is None:
            return

        dialog.search_button.setEnabled(True)
        dialog.save_all_button.setEnabled(True)
        dialog.search_button.setText("Search")
        dialog.set_results(stations)

        if error and stations:
            dialog.message.setText(
                f"Found {len(stations)} stations. "
                f"Some server requests failed: {error}"
            )
        elif error:
            dialog.message.setText(f"Search failed: {error}")
        elif not stations:
            dialog.message.setText("No stations found.")

    @staticmethod
    def station_line(station):
        url = (
            station.get("url_resolved")
            or station.get("url")
            or ""
        ).strip()
        name = (station.get("name") or url).strip()
        tags = (station.get("tags") or "").strip()

        if not url:
            return None

        url = url.replace("|", "%7C").replace("\n", "")
        name = name.replace("|", " ").replace("\n", " ")
        tags = tags.replace("|", ", ").replace("\n", " ")
        return f"{url}|{name}|{tags}"

    def add_search_result(self, dialog):
        station = dialog.selected_station()
        if not station:
            QMessageBox.information(
                dialog,
                "Search",
                "Select a station first.",
            )
            return

        line = self.station_line(station)
        if not line:
            QMessageBox.warning(
                dialog,
                "Search",
                "This station has no stream URL.",
            )
            return

        try:
            with self.playlist_path().open("a", encoding="utf-8") as file:
                file.write(line + "\n")
        except OSError as exc:
            QMessageBox.critical(
                dialog,
                "Error",
                f"Could not save station:\n{exc}",
            )
            return

        self.load_playlist()
        dialog.message.setText(
            f"Station added to playlist '{self.current_playlist}'."
        )

    def save_search_results(self, dialog):
        stations = []

        for row in range(dialog.table.rowCount()):
            item = dialog.table.item(row, 0)
            if item:
                station = item.data(Qt.ItemDataRole.UserRole)
                if station:
                    stations.append(station)

        if not stations:
            QMessageBox.information(
                dialog,
                "Playlist",
                "There are no search results to save.",
            )
            return

        name, accepted = QInputDialog.getText(
            dialog,
            "Save All Stations",
            f"New playlist name ({len(stations)} stations):",
        )
        if not accepted:
            return

        name = re.sub(r"[^\w\- ]+", "", name.strip(), flags=re.UNICODE)
        name = re.sub(r"\s+", "_", name)
        if not name:
            return

        path = PLAYLIST_DIR / f"playlist.{name}"
        if path.exists():
            QMessageBox.warning(
                dialog,
                "Playlist",
                "A playlist with that name already exists.",
            )
            return

        lines = []
        seen = set()

        for station in stations:
            line = self.station_line(station)
            if line:
                url = line.split("|", 1)[0]
                if url not in seen:
                    seen.add(url)
                    lines.append(line)

        try:
            path.write_text(
                "\n".join(lines) + ("\n" if lines else ""),
                encoding="utf-8",
            )
        except OSError as exc:
            QMessageBox.critical(
                dialog,
                "Error",
                f"Could not create playlist:\n{exc}",
            )
            return

        self.refresh_playlists(select=name)
        self.load_playlist()
        QMessageBox.information(
            dialog,
            "Done",
            f"Created playlist '{name}' with {len(lines)} stations.",
        )

    def add_custom_station(self):
        dialog = QDialog(self)
        dialog.setWindowTitle("Add Station")

        name_edit = QLineEdit()
        url_edit = QLineEdit()
        description_edit = QLineEdit()

        form = QFormLayout(dialog)
        form.addRow("Name:", name_edit)
        form.addRow("Stream URL:", url_edit)
        form.addRow("Description / tags:", description_edit)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        form.addRow(buttons)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)

        if dialog.exec() != QDialog.DialogCode.Accepted:
            return

        name = name_edit.text().strip()
        url = url_edit.text().strip()
        description = description_edit.text().strip()

        if not name or not url:
            QMessageBox.warning(
                self,
                "YRadio",
                "Enter both a name and a URL.",
            )
            return

        self.append_station(
            self.playlist_path(),
            url,
            name,
            description,
        )
        self.load_playlist()

    @staticmethod
    def append_station(path, url, name, description=""):
        url = url.replace("|", "%7C").replace("\n", "")
        name = name.replace("|", " ").replace("\n", " ")
        description = description.replace("|", ", ").replace("\n", " ")

        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as file:
                file.write(f"{url}|{name}|{description}\n")
        except OSError as exc:
            QMessageBox.warning(None, "Error", str(exc))

    def remove_selected(self):
        station = self.selected_playlist_station()
        if not station:
            QMessageBox.information(
                self,
                "YRadio",
                "Select a station first.",
            )
            return

        answer = QMessageBox.question(
            self,
            "Remove Station",
            f"Remove '{station[1]}' from the current playlist?",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return

        try:
            lines = self.playlist_path().read_text(
                encoding="utf-8",
                errors="replace",
            ).splitlines()

            removed = False
            kept = []

            for line in lines:
                fields = line.split("|", 2)
                if (
                    not removed
                    and len(fields) >= 2
                    and fields[0].strip() == station[0]
                ):
                    removed = True
                    continue
                kept.append(line)

            self.playlist_path().write_text(
                "\n".join(kept) + ("\n" if kept else ""),
                encoding="utf-8",
            )
        except OSError as exc:
            QMessageBox.warning(self, "Error", str(exc))
            return

        self.load_playlist()

    def create_playlist(self):
        name, accepted = QInputDialog.getText(
            self,
            "New Playlist",
            "Playlist name:",
        )
        if not accepted:
            return

        name = re.sub(r"[^\w\- ]+", "", name.strip(), flags=re.UNICODE)
        name = re.sub(r"\s+", "_", name)
        if not name:
            return

        path = PLAYLIST_DIR / f"playlist.{name}"
        if path.exists():
            QMessageBox.warning(
                self,
                "Playlist",
                "A playlist with that name already exists.",
            )
            return

        try:
            path.touch()
        except OSError as exc:
            QMessageBox.warning(self, "Error", str(exc))
            return

        self.refresh_playlists(select=name)
        self.load_playlist()

    def copy_selected_to_playlist(self):
        station = self.selected_playlist_station()
        if not station:
            QMessageBox.information(
                self,
                "YRadio",
                "Select a station first.",
            )
            return

        filename, _ = QFileDialog.getSaveFileName(
            self,
            "Choose Playlist",
            str(PLAYLIST_DIR / "playlist.Favorites"),
            "YRadio playlists (playlist.*)",
        )
        if not filename:
            return

        path = Path(filename)
        if not path.name.startswith("playlist."):
            path = path.with_name("playlist." + path.name)

        self.append_station(path, *station)
        self.refresh_playlists()

    def get_volume(self):
        if shutil.which("pactl"):
            code, output, _ = run_command(
                ["pactl", "get-sink-volume", "@DEFAULT_SINK@"]
            )
            match = re.search(r"(\d{1,3})%", output) if code == 0 else None
            if match:
                return max(0, min(100, int(match.group(1))))

        if shutil.which("amixer"):
            code, output, _ = run_command(
                ["amixer", "-M", "get", "Master"]
            )
            values = re.findall(r"\[(\d{1,3})%\]", output) if code == 0 else []
            if values:
                return max(0, min(100, int(values[-1])))

        return None

    def show_volume(self):
        value = self.get_volume()
        if value is None:
            QMessageBox.warning(
                self,
                "Volume",
                "Neither pactl nor amixer was found.",
            )
            return

        if self.volume_dialog and self.volume_dialog.isVisible():
            self.volume_dialog.close()
            return

        self.volume_dialog = VolumeDialog(self, value, self.set_volume)
        center = self.mapToGlobal(self.rect().center())
        self.volume_dialog.move(
            center - self.volume_dialog.rect().center()
        )
        self.volume_dialog.show()

    def set_volume(self, value):
        if shutil.which("pactl"):
            code, _, _ = run_command([
                "pactl",
                "set-sink-volume",
                "@DEFAULT_SINK@",
                f"{value}%",
            ])
            if code == 0:
                return

        if shutil.which("amixer"):
            run_command([
                "amixer",
                "-M",
                "sset",
                "Master",
                f"{value}%",
            ])

    def toggle_ripping(self):
        if self.ripper.state() != QProcess.ProcessState.NotRunning:
            self.ripper.terminate()
            if not self.ripper.waitForFinished(1200):
                self.ripper.kill()

            self.rip_button.setText("Start Recording")
            self.status.setText("Recording stopped")
            return

        if self.player_name != "mpv":
            QMessageBox.information(
                self,
                "Recording",
                "mpv is required for recording.",
            )
            return

        url = self.current_url
        if not url and NOW_URL.exists():
            try:
                url = NOW_URL.read_text(encoding="utf-8").strip()
            except OSError:
                url = ""

        if not url:
            QMessageBox.information(
                self,
                "Recording",
                "Start a station before recording.",
            )
            return

        lua_script = APP_DIR / "yradio_ripper.lua"
        if not lua_script.is_file():
            QMessageBox.warning(
                self,
                "Recording",
                f"Script not found:\n{lua_script}",
            )
            return

        try:
            recording_dir = RIP_CONF.read_text(
                encoding="utf-8",
                errors="replace",
            ).strip()
        except OSError:
            recording_dir = ""

        if not recording_dir:
            recording_dir = QFileDialog.getExistingDirectory(
                self,
                "Choose Recording Folder",
                str(HOME),
            )
            if not recording_dir:
                return

            try:
                RIP_CONF.parent.mkdir(parents=True, exist_ok=True)
                RIP_CONF.write_text(
                    recording_dir + "\n",
                    encoding="utf-8",
                )
            except OSError as exc:
                QMessageBox.warning(self, "Recording", str(exc))
                return

        program = shutil.which("mpv")
        if not program:
            QMessageBox.warning(
                self,
                "Recording",
                "mpv was not found.",
            )
            return

        self.ripper.start(
            program,
            [
                "--ao=null",
                f"--script={lua_script}",
                "--vo=null",
                url,
            ],
        )
        self.rip_button.setText("Stop Recording")
        self.status.setText(f"Recording: {self.current_station}")

    def ripper_finished(self, _code, _status):
        self.rip_button.setText("Start Recording")

    def closeEvent(self, event):
        if self.ripper.state() != QProcess.ProcessState.NotRunning:
            self.ripper.terminate()
            if not self.ripper.waitForFinished(1000):
                self.ripper.kill()

        self.stop_playback()
        self.tray.hide()
        event.accept()

def main():
    app = QApplication(sys.argv)
    app.setApplicationName("YRadio")

    if not shutil.which("mpv") and not shutil.which("mplayer"):
        QMessageBox.critical(
            None,
            "YRadio",
            "mpv or mplayer was not found. Install one of these players.",
        )
        return 1

    window = YRadioWindow()
    window.show()
    return app.exec()

if __name__ == "__main__":
    sys.exit(main())
