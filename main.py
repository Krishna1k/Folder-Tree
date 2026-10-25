from kivy.config import Config

Config.set("graphics", "maxfps", "120")
Config.set("graphics", "vsync", "1")

import traceback
from kivy.app import App
from kivy.uix.label import Label
from kivy.uix.scrollview import ScrollView


def make_error_view(err_text):
    sv = ScrollView()
    lb = Label(
        text=err_text or "Unknown Error",
        size_hint_y=None,
        font_size="12sp",
        halign="left",
        valign="top",
        padding=(20, 20),
    )
    lb.bind(
        width=lambda *a: setattr(lb, "text_size", (lb.width, None)),
        texture_size=lambda *a: setattr(lb, "height", lb.texture_size[1]),
    )
    sv.add_widget(lb)
    return sv


ERR = ""
try:
    from persist import PersistApp
except Exception:
    ERR = traceback.format_exc()


class ErrApp(App):
    def build(self):
        return make_error_view(ERR)


if ERR:
    ErrApp().run()
else:
    orig_build = PersistApp.build

    def safe_build(self):
        try:
            return orig_build(self)
        except Exception:
            return make_error_view(traceback.format_exc())

    PersistApp.build = safe_build
    PersistApp().run()
