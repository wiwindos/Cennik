"""Exercise the simplified dialog on an isolated profile, without game input."""
import json
import tempfile
import time
import tkinter as tk
from pathlib import Path
from types import SimpleNamespace

from PIL import ImageGrab

from cennik.collector.desktop import CollectorWindow
from cennik.worker.agent import WorkerAgent
from cennik.worker.window_picker import WindowPicker


def widgets(parent):
    for child in parent.winfo_children():
        yield child
        yield from widgets(child)


def main():
    output=Path('outputs/worker-0.8.0-dialog')
    output.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='cennik-any-window-') as directory:
        agent=WorkerAgent(Path(directory))
        game=Path(directory)/'game'
        game.mkdir()
        agent.settings.update(game_root=str(game),paused=True)
        agent.manifest={'clients':[],'any_character':True,'session':{'workspace_id':'fixture','sniper_book':{}}}
        target={'hwnd':1,'pid':2,'started':3,'path':'fixture/Wow.exe','title':'Тестовое окно',
                'monitor':'DISPLAY2','x':-1920,'y':0,'width':960,'height':720}
        agent.cycle.backend=SimpleNamespace(windows=lambda root:[target])
        agent.write_import=lambda:None
        agent.tick=lambda **kwargs:None
        agent.drive_controls=lambda:None
        app=CollectorWindow(agent,worker=True)
        try:
            app.root.update()
            assert not agent.cycle.settings['enabled']
            assert 'без выбора окна' in app.agent.overview()['notice']
            app.root.after(100,lambda:None)
            ImageGrab.grab(window=app.root.winfo_id()).save(output/'main.png')
            app.cycle_dialog()
            app.root.update()
            dialog=next(w for w in app.root.winfo_children() if isinstance(w,tk.Toplevel))
            controls=list(widgets(dialog))
            assert not any(w.winfo_class()=='TCombobox' and 'Персонаж' in str(w) for w in controls)
            picker=next(w for w in controls if isinstance(w,WindowPicker))
            picker.choice.current(0)
            picker.choose()
            buttons=[w for w in controls if w.winfo_class()=='TButton']
            next(w for w in buttons if w.cget('text')=='Запустить').invoke()
            deadline=time.monotonic()+5
            while not agent.cycle.settings.get('any_character') and time.monotonic()<deadline:
                app.root.update()
                time.sleep(.05)
            assert agent.cycle.settings['any_character'] and agent.cycle.settings['client_id'] is None
            start=next(w for w in buttons if w.cget('text')=='Запустить')
            while str(start.cget('state'))=='disabled' and time.monotonic()<deadline:
                app.root.update()
                time.sleep(.05)
            assert str(start.cget('state'))=='normal'
            app.root.update()
            ImageGrab.grab(window=dialog.winfo_id()).save(output/'dialog.png')
            next(w for w in buttons if w.cget('text')=='Остановить').invoke()
            assert not agent.cycle.settings['enabled']
            print(json.dumps({'simple_start':'passed','no_roles_required':True,'stop':'passed'}))
        finally:
            app.quit()


if __name__=='__main__':
    main()
