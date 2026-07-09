use std::sync::Arc;
use parking_lot::Mutex;
use pyo3::prelude::*;
use crate::io_uring_writer::write_block_uring;

struct PendingWrite {
    fd: i32,
    offset: u64,
    data: Vec<u8>,
    tx: tokio::sync::oneshot::Sender<Result<(String, String), String>>,
}

#[pyclass]
pub struct GroupCommitter {
    queue: Arc<Mutex<Vec<PendingWrite>>>,
    _batch_size: usize,
    _batch_window_ms: u64,
}

#[pymethods]
impl GroupCommitter {
    #[new]
    #[pyo3(signature = (batch_size = 64, batch_window_ms = 4))]
    pub fn new(batch_size: usize, batch_window_ms: u64) -> Self {
        let queue = Arc::new(Mutex::new(Vec::new()));
        
        let q_clone = queue.clone();
        std::thread::spawn(move || {
            loop {
                std::thread::sleep(std::time::Duration::from_millis(batch_window_ms));
                
                let batch = {
                    let mut q = q_clone.lock();
                    if q.is_empty() {
                        continue;
                    }
                    let drain_len = std::cmp::min(q.len(), batch_size);
                    q.drain(0..drain_len).collect::<Vec<PendingWrite>>()
                };

                if batch.is_empty() {
                    continue;
                }

                // Process the batch
                let mut handles = Vec::new();
                let mut unique_fds = Vec::new();

                for item in batch {
                    let fd = item.fd;
                    if !unique_fds.contains(&fd) {
                        unique_fds.push(fd);
                    }

                    let handle = std::thread::spawn(move || {
                        let res = Python::with_gil(|py| {
                            write_block_uring(py, item.fd, item.offset, &item.data)
                        });
                        (res, item.tx)
                    });
                    handles.push(handle);
                }

                // Wait for all writes to finish
                for h in handles {
                    if let Ok((res, tx)) = h.join() {
                        match res {
                            Ok((md5, sha256)) => {
                                let _ = tx.send(Ok((md5, sha256)));
                            }
                            Err(e) => {
                                let _ = tx.send(Err(format!("Write failed: {:?}", e)));
                            }
                        }
                    }
                }

                // Run fdatasync on all unique fds in the batch
                for fd in unique_fds {
                    unsafe { libc::fdatasync(fd) };
                }
            }
        });

        Self {
            queue,
            _batch_size: batch_size,
            _batch_window_ms: batch_window_ms,
        }
    }

    /// Submit a write request. Blocks the current thread, releasing the GIL.
    pub fn submit(
        &self,
        py: Python<'_>,
        fd: i32,
        offset: u64,
        data: &[u8],
    ) -> PyResult<(String, String)> {
        let (tx, rx) = tokio::sync::oneshot::channel();
        let data_vec = data.to_vec();

        {
            let mut q = self.queue.lock();
            q.push(PendingWrite {
                fd,
                offset,
                data: data_vec,
                tx,
            });
        }

        py.allow_threads(move || {
            match rx.blocking_recv() {
                Ok(Ok(res)) => Ok(res),
                Ok(Err(e)) => Err(pyo3::exceptions::PyOSError::new_err(e)),
                Err(_) => Err(pyo3::exceptions::PyOSError::new_err("Recv error")),
            }
        })
    }
}
