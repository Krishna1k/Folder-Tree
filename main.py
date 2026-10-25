import traceback
from kivy.app import App
from kivy.uix.label import Label
from kivy.uix.scrollview import ScrollView

ERR = ""
try:
    from persist import PersistApp
except Exception:
    ERR = traceback.format_exc()


class ErrApp(App):
    def build(self):
        sv = ScrollView()
        lb = Label(text=ERR or "Unknown Error", size_hint_y=None,
                   font_size="12sp", halign="left", valign="top")
        lb.bind(width=lambda *a: setattr(lb, "text_size", (lb.width, None)),
                texture_size=lambda *a: setattr(lb, "height",
                                                lb.texture_size[1]))
        sv.add_widget(lb)
        return sv


if ERR:
    ErrApp().run()
else:
    try:
        PersistApp().run()
    except Exception:
        ERR = traceback.format_exc()
        ErrApp().run()
