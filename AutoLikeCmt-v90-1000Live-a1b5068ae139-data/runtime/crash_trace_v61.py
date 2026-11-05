import faulthandler, os, time, sys, threading, traceback
from pathlib import Path
_file=None
def install():
    global _file
    folder=Path(__file__).parent/'logs';folder.mkdir(exist_ok=True)
    _file=open(folder/'native-crash.log','a',encoding='utf-8')
    _file.write('\nSTART '+time.strftime('%Y-%m-%d %H:%M:%S')+'\n');_file.flush()
    faulthandler.enable(file=_file,all_threads=True)
    def trace_workers():
        while True:
            time.sleep(30)
            frames=sys._current_frames()
            _file.write('\nWORKER SNAPSHOT '+time.strftime('%H:%M:%S')+'\n')
            count=0
            for tid,frame in frames.items():
                stack=traceback.extract_stack(frame)
                if not any('pool_scheduler.py' in row.filename or row.filename.replace('\\','/').endswith('client/app/runner.py') for row in stack):continue
                count+=1
                _file.write('Thread '+str(tid)+'\n'+''.join(traceback.format_list(stack[-22:])))
            _file.write('WORKERS '+str(count)+'\n');_file.flush()
    threading.Thread(target=trace_workers,daemon=True,name='v61-worker-diagnostic').start()
    from client.app.bridge import Bridge
    for name in ('start_run','_run_and_report'):
        previous=getattr(Bridge,name)
        def wrap(previous,name):
            def call(self,*a,**k):
                _file.write('ENTER '+name+'\n');_file.flush()
                # Worker snapshots below capture hangs without an extra native dump timer.
                try: result=previous(self,*a,**k)
                finally:
                    pass
                _file.write('EXIT '+name+'\n');_file.flush()
                return result
            return call
        setattr(Bridge,name,wrap(previous,name))

    import run_lifecycle_guard
    run_lifecycle_guard.install()
