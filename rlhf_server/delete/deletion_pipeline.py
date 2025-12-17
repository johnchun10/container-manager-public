from typing import Dict, Deque, List
from terminal_bench.terminal import Terminal
from threading import Lock, Thread, Semaphore
import time
from collections import deque

MAX_PARALLELISM = 20
WAIT_TIME_S = 0.25

class DeletionRequest:
    def __init__(self, terminal_id: str):
        self.terminal_id = terminal_id
        self.done = False

class DeletionPipeline:

    def __init__(self,
                 terminals: Dict[str, Terminal], 
                 state_lock: Lock,):
        
        self.state_lock = state_lock
        self.terminals = terminals

        self.deletion_queue: Deque[DeletionRequest] = deque()
        self.deletion_q_lock: Lock = Lock()

        self.deletion_semaphore: Semaphore = Semaphore(MAX_PARALLELISM)

        self.deletion_thread = Thread(target=self.deletion_worker, daemon=True)
        self.deletion_thread.start()

    
    def deletion_worker(self):
        while True:

            del_req = None

            with self.deletion_q_lock:
                if len(self.deletion_queue) > 0:
                    del_req = self.deletion_queue.popleft()
            
            if del_req != None:
                self.deletion_semaphore.acquire()
                thread = Thread(target=self.single_terimnal_deletor, args=(del_req, ))
                thread.start()
            else:
                time.sleep(WAIT_TIME_S)
    

    def single_terimnal_deletor(self, del_req: DeletionRequest):
        try:
            st = time.perf_counter()
            with self.state_lock:
                terminal = self.terminals[del_req.terminal_id]
                
            terminal.stop()
            et = time.perf_counter()
            print(f'Time taken to delete : {del_req.terminal_id} is {et - st}')

        except Exception as e:
            print(f'Ran into error while stopping terminal: {del_req.terminal_id}')

        finally:
            del_req.done = True
            self.deletion_semaphore.release()        
    

    
    def stop(self, terminal_id: str):
        del_req = DeletionRequest(terminal_id=terminal_id)
        with self.deletion_q_lock:
            self.deletion_queue.append(del_req)
            print(f'Added deletion request and the length is: {len(self.deletion_queue)}')
        
        while not del_req.done:
            time.sleep(WAIT_TIME_S)

