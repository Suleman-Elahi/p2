use hmac::{Hmac, Mac};
use md5::Md5;
use sha2::{Digest, Sha256};
use aes_gcm::{
    aead::{KeyInit, AeadInPlace},
    Aes256Gcm, Nonce, Tag,
};
use pyo3::prelude::*;
use pyo3::types::PyBytes;

type HmacSha256 = Hmac<Sha256>;

pub fn hmac_bytes(key: &[u8], msg: &[u8]) -> Vec<u8> {
    let mut mac = <HmacSha256 as hmac::Mac>::new_from_slice(key).expect("HMAC accepts any key length");
    mac.update(msg);
    mac.finalize().into_bytes().to_vec()
}

#[pyfunction]
pub fn derive_signing_key<'py>(
    py: Python<'py>,
    secret_key: &str,
    date: &str,
    region: &str,
    service: &str,
) -> Bound<'py, PyBytes> {
    let k_secret = format!("AWS4{}", secret_key);
    let k_date = hmac_bytes(k_secret.as_bytes(), date.as_bytes());
    let k_region = hmac_bytes(&k_date, region.as_bytes());
    let k_service = hmac_bytes(&k_region, service.as_bytes());
    let k_signing = hmac_bytes(&k_service, b"aws4_request");
    PyBytes::new(py, &k_signing)
}

#[pyfunction]
pub fn hmac_sha256_hex(key: &[u8], msg: &str) -> String {
    hex::encode(hmac_bytes(key, msg.as_bytes()))
}

#[pyfunction]
pub fn hmac_sha256_bytes<'py>(py: Python<'py>, key: &[u8], msg: &str) -> Bound<'py, PyBytes> {
    PyBytes::new(py, &hmac_bytes(key, msg.as_bytes()))
}

#[pyfunction]
pub fn md5_hex(data: &[u8]) -> String {
    hex::encode(Md5::digest(data))
}

#[pyfunction]
pub fn md5_bytes<'py>(py: Python<'py>, data: &[u8]) -> Bound<'py, PyBytes> {
    PyBytes::new(py, &Md5::digest(data))
}

#[pyfunction]
pub fn write_and_hash_small(py: Python<'_>, path: &str, data: &[u8]) -> pyo3::PyResult<(String, String)> {
    use std::fs::File;
    use std::io::Write;

    let data_copy = data.to_vec();
    let path_owned = path.to_string();

    let (md5_hex, sha256_hex) = py.allow_threads(move || {
        let mut f = File::create(&path_owned)?;
        f.write_all(&data_copy)?;

        let md5_hex = hex::encode(Md5::digest(&data_copy));
        let sha256_hex = hex::encode(Sha256::digest(&data_copy));
        Ok::<(String, String), std::io::Error>((md5_hex, sha256_hex))
    })?;

    Ok((md5_hex, sha256_hex))
}

/// AES-GCM-256 Encryption.
/// Returns (ciphertext, tag_bytes)
#[pyfunction]
pub fn aes_gcm_encrypt<'py>(
    py: Python<'py>,
    key: &[u8],
    nonce: &[u8],
    plaintext: &[u8],
) -> PyResult<(Bound<'py, PyBytes>, Bound<'py, PyBytes>)> {
    if key.len() != 32 {
        return Err(pyo3::exceptions::PyValueError::new_err("AES-GCM-256 key must be 32 bytes"));
    }
    if nonce.len() != 12 {
        return Err(pyo3::exceptions::PyValueError::new_err("AES-GCM-256 nonce must be 12 bytes"));
    }

    let key_arr = aes_gcm::Key::<Aes256Gcm>::from_slice(key);
    let cipher = Aes256Gcm::new(key_arr);
    let nonce_arr = Nonce::from_slice(nonce);

    let mut buffer = plaintext.to_vec();

    let tag_bytes = py.allow_threads(|| {
        cipher.encrypt_in_place_detached(nonce_arr, &[], &mut buffer)
            .map(|tag| tag.to_vec())
            .map_err(|e| pyo3::exceptions::PyValueError::new_err(format!("Encryption failed: {:?}", e)))
    })?;

    Ok((PyBytes::new(py, &buffer), PyBytes::new(py, &tag_bytes)))
}

/// AES-GCM-256 Decryption.
/// Returns decrypted plaintext
#[pyfunction]
pub fn aes_gcm_decrypt<'py>(
    py: Python<'py>,
    key: &[u8],
    nonce: &[u8],
    ciphertext: &[u8],
    tag: &[u8],
) -> PyResult<Bound<'py, PyBytes>> {
    if key.len() != 32 {
        return Err(pyo3::exceptions::PyValueError::new_err("AES-GCM-256 key must be 32 bytes"));
    }
    if nonce.len() != 12 {
        return Err(pyo3::exceptions::PyValueError::new_err("AES-GCM-256 nonce must be 12 bytes"));
    }
    if tag.len() != 16 {
        return Err(pyo3::exceptions::PyValueError::new_err("AES-GCM-256 tag must be 16 bytes"));
    }

    let key_arr = aes_gcm::Key::<Aes256Gcm>::from_slice(key);
    let cipher = Aes256Gcm::new(key_arr);
    let nonce_arr = Nonce::from_slice(nonce);
    let tag_arr = Tag::from_slice(tag);

    let mut buffer = ciphertext.to_vec();

    py.allow_threads(|| {
        cipher.decrypt_in_place_detached(nonce_arr, &[], &mut buffer, tag_arr)
            .map_err(|e| pyo3::exceptions::PyValueError::new_err(format!("Decryption failed: {:?}", e)))
    })?;

    Ok(PyBytes::new(py, &buffer))
}
