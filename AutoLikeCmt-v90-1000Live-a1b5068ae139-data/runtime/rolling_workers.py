from concurrent.futures import ThreadPoolExecutor,wait,FIRST_COMPLETED

def run_rolling(items,workers,invoke,should_stop,on_error):
    iterator=iter(items)
    with ThreadPoolExecutor(max_workers=workers) as executor:
        pending=set()
        def submit():
            if should_stop():return False
            try:item=next(iterator)
            except StopIteration:return False
            pending.add(executor.submit(invoke,item));return True
        for _ in range(workers):
            if not submit():break
        while pending:
            completed,_=wait(pending,timeout=.25,return_when=FIRST_COMPLETED)
            for future in completed:
                pending.remove(future)
                try:future.result()
                except Exception as error:on_error(error)
                submit()
