[app]
title = Persist
package.name = persist
package.domain = org.persist
source.dir = .
source.include_exts = py
version = 1.0
requirements = python3,kivy==2.3.0,kivymd==1.2.0,pillow,pyjnius,android
orientation = portrait
fullscreen = 0
android.permissions = READ_EXTERNAL_STORAGE,WRITE_EXTERNAL_STORAGE,MANAGE_EXTERNAL_STORAGE,POST_NOTIFICATIONS,VIBRATE,WAKE_LOCK,REQUEST_IGNORE_BATTERY_OPTIMIZATIONS
android.api = 33
android.minapi = 24
android.archs = arm64-v8a
android.accept_sdk_license = True
android.ndk = 25b
p4a.branch = v2024.01.21

[buildozer]
log_level = 2
warn_on_root = 1
