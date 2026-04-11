/// p2_s3_crypto — PyO3 Rust extension for AWS Signature v4 HMAC computation.
///
/// Exposes two functions to Python:
///   derive_signing_key(secret_key, date, region, service) -> bytes
///   hmac_sha256_hex(key: bytes, msg: str) -> str
///
/// These are the hot path in AWS v4 presigned URL validation — pure HMAC-SHA256
/// key derivation that runs on every authenticated S3 request.
use hmac::{Hmac, Mac};
use md5::Md5;
use pyo3::prelude::*;
use pyo3::types::PyBytes;
use sha2::{Digest, Sha256};

type HmacSha256 = Hmac<Sha256>;

fn hmac_bytes(key: &[u8], msg: &[u8]) -> Vec<u8> {
    let mut mac = HmacSha256::new_from_slice(key).expect("HMAC accepts any key length");
    mac.update(msg);
    mac.finalize().into_bytes().to_vec()
}

/// Derive the AWS v4 signing key from the secret key, date, region, and service.
/// Equivalent to:
///   kDate    = HMAC("AWS4" + secret_key, date)
///   kRegion  = HMAC(kDate, region)
///   kService = HMAC(kRegion, service)
///   kSigning = HMAC(kService, "aws4_request")
#[pyfunction]
fn derive_signing_key<'py>(
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

/// Compute HMAC-SHA256 of msg using key (bytes), return lowercase hex string.
#[pyfunction]
fn hmac_sha256_hex(key: &[u8], msg: &str) -> String {
    hex::encode(hmac_bytes(key, msg.as_bytes()))
}

/// Compute HMAC-SHA256 of msg using key (bytes), return raw bytes.
#[pyfunction]
fn hmac_sha256_bytes<'py>(py: Python<'py>, key: &[u8], msg: &str) -> Bound<'py, PyBytes> {
    PyBytes::new(py, &hmac_bytes(key, msg.as_bytes()))
}

/// Compute MD5 of data, return lowercase hex string.
/// Used for S3 ETag computation.
#[pyfunction]
fn md5_hex(data: &[u8]) -> String {
    hex::encode(Md5::digest(data))
}

/// Compute MD5 of data, return raw 16 bytes.
#[pyfunction]
fn md5_bytes<'py>(py: Python<'py>, data: &[u8]) -> Bound<'py, PyBytes> {
    PyBytes::new(py, &Md5::digest(data))
}

/// Write small payload to disk and compute hashes sequentially in C/Rust speed.
#[pyfunction]
fn write_and_hash_small(path: &str, data: &[u8]) -> pyo3::PyResult<(String, String)> {
    use std::fs::File;
    use std::io::Write;
    
    // Write directly
    let mut f = File::create(path)?;
    f.write_all(data)?;
    
    // Hash
    let md5_hex = hex::encode(Md5::digest(data));
    let sha256_hex = hex::encode(Sha256::digest(data));
    Ok((md5_hex, sha256_hex))
}

#[pymodule]
fn p2_s3_crypto(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(derive_signing_key, m)?)?;
    m.add_function(wrap_pyfunction!(hmac_sha256_hex, m)?)?;
    m.add_function(wrap_pyfunction!(hmac_sha256_bytes, m)?)?;
    m.add_function(wrap_pyfunction!(md5_hex, m)?)?;
    m.add_function(wrap_pyfunction!(md5_bytes, m)?)?;
    m.add_function(wrap_pyfunction!(write_and_hash_small, m)?)?;
    Ok(())
}
