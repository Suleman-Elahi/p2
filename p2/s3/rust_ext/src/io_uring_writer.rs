use std::os::unix::fs::FileExt;
use std::fs::File;
use std::os::unix::io::FromRawFd;
use md5::Md5;
use sha2::{Digest, Sha256};
use pyo3::prelude::*;

#[cfg(feature = "io_uring")]
use std::sync::OnceLock;
#[cfg(feature = "io_uring")]
use tokio::sync::oneshot;
#[cfg(feature = "io_uring")]
use crossbeam_channel::Sender;

#[cfg(feature = "io_uring")]
struct WriteJob {
    fd: i32,
    offset: u64,
    data: Vec<u8>,
    tx: oneshot::Sender<std::io::Result<()>>,
}

#[cfg(feature = "io_uring")]
static URING_WRITER_CHANNEL: OnceLock<Sender<WriteJob>> = OnceLock::new();

#[cfg(feature = "io_uring")]
fn init_uring_writer() -> Sender<WriteJob> {
    let (tx, rx) = crossbeam_channel::unbounded::<WriteJob>();
    std::thread::spawn(move || {
        tokio_uring::start(async move {
            while let Ok(job) = rx.recv() {
                let dup_fd = unsafe { libc::dup(job.fd) };
                if dup_fd < 0 {
                    let _ = job.tx.send(Err(std::io::Error::last_os_error()));
                    continue;
                }
                let file = unsafe { File::from_raw_fd(dup_fd) };
                let ufile = tokio_uring::fs::File::from_std(file);
                
                let (res, _) = ufile.write_all_at(job.data, job.offset).await;
                // ufile will drop and close dup_fd safely, leaving job.fd open
                let _ = job.tx.send(res);
            }
        });
    });
    tx
}

/// Write data to a volume file descriptor at a given offset and compute hashes.
/// Releases the GIL to perform I/O and hashing concurrently.
#[pyfunction]
pub fn write_block_uring(
    py: Python<'_>,
    fd: i32,
    offset: u64,
    data: &[u8],
) -> PyResult<(String, String)> {
    let data_vec = data.to_vec();

    py.allow_threads(move || {
        // Compute hashes in parallel/GIL-free
        let md5_hex = hex::encode(Md5::digest(&data_vec));
        let sha256_hex = hex::encode(Sha256::digest(&data_vec));

        #[cfg(feature = "io_uring")]
        {
            let chan = URING_WRITER_CHANNEL.get_or_init(init_uring_writer);
            let (tx, rx) = oneshot::channel();
            let job = WriteJob {
                fd,
                offset,
                data: data_vec.clone(),
                tx,
            };
            if chan.send(job).is_ok() {
                // Wait for the io_uring write to complete
                // Since this runs in allow_threads, it does not block the Python GIL
                match rx.blocking_recv() {
                    Ok(Ok(())) => return Ok((md5_hex, sha256_hex)),
                    Ok(Err(e)) => return Err(pyo3::exceptions::PyOSError::new_err(e)),
                    Err(_) => {}
                }
            }
        }

        // Fallback or if io_uring is disabled
        let file = unsafe { File::from_raw_fd(fd) };
        let res = file.write_all_at(&data_vec, offset);
        std::mem::forget(file); // Do not close the fd!

        match res {
            Ok(()) => Ok((md5_hex, sha256_hex)),
            Err(e) => Err(pyo3::exceptions::PyOSError::new_err(e)),
        }
    })
}

/// Call fdatasync on the given file descriptor.
#[pyfunction]
pub fn fdatasync_uring(py: Python<'_>, fd: i32) -> PyResult<()> {
    py.allow_threads(move || {
        let res = unsafe { libc::fdatasync(fd) };
        if res == 0 {
            Ok(())
        } else {
            Err(pyo3::exceptions::PyOSError::new_err(std::io::Error::last_os_error()))
        }
    })
}
