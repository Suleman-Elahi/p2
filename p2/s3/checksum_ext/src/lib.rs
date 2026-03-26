use base64::{engine::general_purpose::STANDARD, Engine};
use crc32fast::Hasher as Crc32Hasher;
use pyo3::prelude::*;
use sha1::Sha1;
use sha2::{Digest, Sha256};

#[pyfunction]
fn verify_crc32(data: &[u8], expected_b64: &str) -> bool {
    compute_crc32(data) == expected_b64
}

#[pyfunction]
fn verify_crc32c(data: &[u8], expected_b64: &str) -> bool {
    compute_crc32c(data) == expected_b64
}

#[pyfunction]
fn verify_sha256(data: &[u8], expected_hex: &str) -> bool {
    compute_sha256(data) == expected_hex
}

#[pyfunction]
fn verify_sha1(data: &[u8], expected_b64: &str) -> bool {
    compute_sha1(data) == expected_b64
}

#[pyfunction]
fn compute_crc32(data: &[u8]) -> String {
    let mut h = Crc32Hasher::new();
    h.update(data);
    STANDARD.encode(h.finalize().to_be_bytes())
}

#[pyfunction]
fn compute_crc32c(data: &[u8]) -> String {
    STANDARD.encode(crc32c::crc32c(data).to_be_bytes())
}

#[pyfunction]
fn compute_sha256(data: &[u8]) -> String {
    hex::encode(Sha256::digest(data))
}

#[pyfunction]
fn compute_sha1(data: &[u8]) -> String {
    STANDARD.encode(Sha1::digest(data))
}

#[pymodule]
fn p2_s3_checksum(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(verify_crc32, m)?)?;
    m.add_function(wrap_pyfunction!(verify_crc32c, m)?)?;
    m.add_function(wrap_pyfunction!(verify_sha256, m)?)?;
    m.add_function(wrap_pyfunction!(verify_sha1, m)?)?;
    m.add_function(wrap_pyfunction!(compute_crc32, m)?)?;
    m.add_function(wrap_pyfunction!(compute_crc32c, m)?)?;
    m.add_function(wrap_pyfunction!(compute_sha256, m)?)?;
    m.add_function(wrap_pyfunction!(compute_sha1, m)?)?;
    Ok(())
}
