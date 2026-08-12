# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for DocSorter. 需求 18.6。

--noconsole：Windows 桌面工具不该弹出控制台窗口。
--add-data：把随包的 rules_default.yaml 纳入打包产物，首次运行时释放。
裁剪未使用的 Qt 模块以控制体积。
"""

block_cipher = None

a = Analysis(
    ['app/main.py'],
    pathex=[],
    binaries=[],
    datas=[
        # 需求 18.6：把 rules_default.yaml 纳入包内
        ('app/config/rules_default.yaml', 'app/config'),
    ],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # 裁剪未使用的 Qt 模块（需求 18.6）
        'Qt3DAnimation', 'Qt3DCore', 'Qt3DExtras', 'Qt3DInput',
        'Qt3DLogic', 'Qt3DRender', 'QtCharts', 'QtDataVisualization',
        'QtMultimedia', 'QtMultimediaWidgets', 'QtNetwork',
        'QtQuick', 'QtQuick3D', 'QtQuickControls2', 'QtQuickWidgets',
        'QtRemoteObjects', 'QtScript', 'QtScriptTools', 'QtScxml',
        'QtSensors', 'QtSerialPort', 'QtSql', 'QtTest',
        'QtWebChannel', 'QtWebEngine', 'QtWebEngineCore',
        'QtWebEngineWidgets', 'QtWebSockets', 'QtXml',
        'QtXmlPatterns',
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='DocSorter',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,  # 需求 18.6：--noconsole
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    # icon='resources/icon.ico',  # 若有图标可取消注释
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='DocSorter',
)
