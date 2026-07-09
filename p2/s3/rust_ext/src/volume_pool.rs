use std::fs::{self, OpenOptions};
use std::os::unix::fs::OpenOptionsExt;
use std::os::unix::io::AsRawFd;
use std::path::Path;
use parking_lot::Mutex;
use pyo3::prelude::*;
use uuid::Uuid;

struct ActiveVolume {
    uuid: String,
    path: String,
    file: std::fs::File,
    write_head: u64,
}

#[pyclass]
pub struct VolumePool {
    vol_dir: String,
    volume_size_bytes: u64,
    pool_size: usize,
    active_volumes: Mutex<Vec<ActiveVolume>>,
}

#[cfg(target_os = "linux")]
fn preallocate_file(fd: std::os::unix::io::RawFd, len: u64) -> std::io::Result<()> {
    let res = unsafe { libc::posix_fallocate(fd, 0, len as libc::off_t) };
    if res == 0 {
        Ok(())
    } else {
        // Fall back to ftruncate if posix_fallocate fails (e.g., due to ENOSPC or filesystem limits)
        let res_trunc = unsafe { libc::ftruncate(fd, len as libc::off_t) };
        if res_trunc == 0 {
            Ok(())
        } else {
            Err(std::io::Error::from_raw_os_error(res))
        }
    }
}

#[cfg(not(target_os = "linux"))]
fn preallocate_file(fd: std::os::unix::io::RawFd, len: u64) -> std::io::Result<()> {
    let res = unsafe { libc::ftruncate(fd, len as libc::off_t) };
    if res == 0 {
        Ok(())
    } else {
        Err(std::io::Error::last_os_error())
    }
}

#[pymethods]
impl VolumePool {
    #[new]
    #[pyo3(signature = (vol_dir, volume_size_bytes = 10737418240, pool_size = 4))]
    pub fn new(vol_dir: String, volume_size_bytes: u64, pool_size: usize) -> PyResult<Self> {
        let path = Path::new(&vol_dir);
        if !path.exists() {
            fs::create_dir_all(path)?;
        }

        let pool = Self {
            vol_dir,
            volume_size_bytes,
            pool_size,
            active_volumes: Mutex::new(Vec::new()),
        };

        pool.initialize_active_pool()?;
        Ok(pool)
    }

    pub fn allocate_block(&self, length: u64) -> PyResult<(String, u64, i32)> {
        let mut active = self.active_volumes.lock();

        // 1. Try to find an active volume with enough space
        for vol in active.iter_mut() {
            if vol.write_head + length <= self.volume_size_bytes {
                let offset = vol.write_head;
                vol.write_head += length;
                return Ok((vol.uuid.clone(), offset, vol.file.as_raw_fd() as i32));
            }
        }

        // 2. No volume has enough space. Rotate: seal the first volume in the list and create a new one.
        if !active.is_empty() {
            let old_vol = active.remove(0);
            self.seal_volume_file(&old_vol.path)?;
        }

        // Create new volume
        let new_vol = self.create_volume()?;
        let uuid = new_vol.uuid.clone();
        let fd = new_vol.file.as_raw_fd() as i32;
        active.push(new_vol);

        let vol_ref = active.last_mut().unwrap();
        let offset = vol_ref.write_head;
        vol_ref.write_head += length;

        Ok((uuid, offset, fd))
    }

    pub fn seal_full_volumes(&self) -> PyResult<Vec<String>> {
        let mut active = self.active_volumes.lock();
        let mut sealed = Vec::new();

        // Check active volumes and seal any that are mostly full (e.g. >95% or no space for average block)
        let mut i = 0;
        while i < active.len() {
            // If less than 1MB remaining, seal it
            if self.volume_size_bytes - active[i].write_head < 1024 * 1024 {
                let vol = active.remove(i);
                self.seal_volume_file(&vol.path)?;
                sealed.push(vol.uuid);
            } else {
                i += 1;
            }
        }

        // Re-fill pool to target pool_size
        while active.len() < self.pool_size {
            active.push(self.create_volume()?);
        }

        Ok(sealed)
    }

    pub fn get_volume_path(&self, vol_uuid: &str) -> String {
        format!("{}/vol_{}.bin", self.vol_dir, vol_uuid)
    }

    pub fn get_active_uuids(&self) -> Vec<String> {
        let active = self.active_volumes.lock();
        active.iter().map(|v| v.uuid.clone()).collect()
    }

    pub fn list_sealed_volumes(&self) -> PyResult<Vec<String>> {
        let mut sealed = Vec::new();
        let entries = fs::read_dir(&self.vol_dir)?;
        for entry in entries {
            let entry = entry?;
            let name = entry.file_name().to_string_lossy().into_owned();
            if name.starts_ok("vol_") && name.ends_with(".bin") {
                let uuid_str = &name[4..name.len() - 4];
                // Check if read-only (sealed) or in active list
                let active = self.get_active_uuids();
                if !active.contains(&uuid_str.to_string()) {
                    sealed.push(uuid_str.to_string());
                }
            }
        }
        Ok(sealed)
    }
}

// Helper methods on VolumePool (internal to Rust)
impl VolumePool {
    fn initialize_active_pool(&self) -> PyResult<()> {
        let mut active = self.active_volumes.lock();
        let entries = fs::read_dir(&self.vol_dir)?;

        // Find existing non-sealed/writable volume files
        for entry in entries {
            let entry = entry?;
            let path = entry.path();
            let name = entry.file_name().to_string_lossy().into_owned();
            if name.starts_with("vol_") && name.ends_with(".bin") {
                let metadata = fs::metadata(&path)?;
                let permissions = metadata.permissions();
                if !permissions.readonly() {
                    let uuid_str = name[4..name.len() - 4].to_string();
                    let file = OpenOptions::new()
                        .read(true)
                        .write(true)
                        .custom_flags(libc::O_DSYNC)
                        .open(&path)?;
                    let size = metadata.len();
                    // We assume write_head is at the current end of file or scan for non-zero bytes (for simplicity, use file size)
                    active.push(ActiveVolume {
                        uuid: uuid_str,
                        path: path.to_string_lossy().into_owned(),
                        file,
                        write_head: size,
                    });
                }
            }
            if active.len() >= self.pool_size {
                break;
            }
        }

        // Fill remaining active pool slots
        while active.len() < self.pool_size {
            active.push(self.create_volume()?);
        }

        Ok(())
    }

    fn create_volume(&self) -> PyResult<ActiveVolume> {
        let uuid_str = Uuid::new_v4().simple().to_string();
        let path_str = self.get_volume_path(&uuid_str);
        let path = Path::new(&path_str);

        // Open with custom O_DSYNC flag for safe group commit writing
        let file = OpenOptions::new()
            .read(true)
            .write(true)
            .create(true)
            .truncate(true)
            .custom_flags(libc::O_DSYNC)
            .open(path)?;

        let fd = file.as_raw_fd();
        preallocate_file(fd, self.volume_size_bytes)?;

        Ok(ActiveVolume {
            uuid: uuid_str,
            path: path_str,
            file,
            write_head: 0,
        })
    }

    fn seal_volume_file(&self, path_str: &str) -> PyResult<()> {
        let path = Path::new(path_str);
        if path.exists() {
            let mut perms = fs::metadata(path)?.permissions();
            perms.set_readonly(true);
            fs::set_permissions(path, perms)?;
        }
        Ok(())
    }
}

// Extends String for starts_with check in list_sealed_volumes
trait StartsOk {
    fn starts_ok(&self, prefix: &str) -> bool;
}
impl StartsOk for String {
    fn starts_ok(&self, prefix: &str) -> bool {
        self.starts_with(prefix)
    }
}
