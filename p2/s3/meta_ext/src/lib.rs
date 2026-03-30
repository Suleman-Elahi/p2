use pyo3::prelude::*;
use pyo3::exceptions::PyValueError;
use redb::{Database, TableDefinition};
use std::sync::Arc;

const OBJECTS_TABLE: TableDefinition<&str, &str> = TableDefinition::new("objects");

#[pyclass]
struct MetaEngine {
    db: Arc<Database>,
}

#[pymethods]
impl MetaEngine {
    #[new]
    fn new(db_path: String) -> PyResult<Self> {
        let db = Database::create(&db_path)
            .map_err(|e| PyValueError::new_err(format!("Failed to create db: {}", e)))?;
        
        // Ensure table exists
        let write_txn = db.begin_write()
            .map_err(|e| PyValueError::new_err(format!("Txn error: {}", e)))?;
        {
            write_txn.open_table(OBJECTS_TABLE)
                .map_err(|e| PyValueError::new_err(format!("Failed to open table: {}", e)))?;
        }
        write_txn.commit()
            .map_err(|e| PyValueError::new_err(format!("Failed to commit db init: {}", e)))?;

        Ok(MetaEngine {
            db: Arc::new(db),
        })
    }

    fn put(&self, path: String, json_metadata: String) -> PyResult<()> {
        let write_txn = self.db.begin_write()
            .map_err(|e| PyValueError::new_err(format!("Txn error: {}", e)))?;
        {
            let mut table = write_txn.open_table(OBJECTS_TABLE)
                .map_err(|e| PyValueError::new_err(format!("Table error: {}", e)))?;
            table.insert(path.as_str(), json_metadata.as_str())
                .map_err(|e| PyValueError::new_err(format!("Write error: {}", e)))?;
        }
        write_txn.commit()
            .map_err(|e| PyValueError::new_err(format!("Commit error: {}", e)))?;
        Ok(())
    }

    fn get(&self, path: String) -> PyResult<Option<String>> {
        let read_txn = self.db.begin_read()
            .map_err(|e| PyValueError::new_err(format!("Read txn error: {}", e)))?;
        let table = read_txn.open_table(OBJECTS_TABLE)
            .map_err(|e| PyValueError::new_err(format!("Table error: {}", e)))?;
        
        let value = table.get(path.as_str())
            .map_err(|e| PyValueError::new_err(format!("Read error: {}", e)))?;
            
        Ok(value.map(|v| v.value().to_string()))
    }

    fn delete(&self, path: String) -> PyResult<()> {
        let write_txn = self.db.begin_write()
            .map_err(|e| PyValueError::new_err(format!("Txn error: {}", e)))?;
        {
            let mut table = write_txn.open_table(OBJECTS_TABLE)
                .map_err(|e| PyValueError::new_err(format!("Table error: {}", e)))?;
            table.remove(path.as_str())
                .map_err(|e| PyValueError::new_err(format!("Delete error: {}", e)))?;
        }
        write_txn.commit()
            .map_err(|e| PyValueError::new_err(format!("Commit error: {}", e)))?;
        Ok(())
    }

    #[pyo3(signature = (prefix=String::new(), start_after=None, max_keys=Some(1000)))]
    fn list(&self, prefix: String, start_after: Option<String>, max_keys: Option<usize>) -> PyResult<Vec<(String, String)>> {
        let read_txn = self.db.begin_read()
            .map_err(|e| PyValueError::new_err(format!("Read txn error: {}", e)))?;
        let table = read_txn.open_table(OBJECTS_TABLE)
            .map_err(|e| PyValueError::new_err(format!("Table error: {}", e)))?;
        
        let mut check_start_after = false;
        let mut start_key = prefix.clone();
        if let Some(ref sa) = start_after {
            if sa > &start_key {
                start_key = sa.clone();
                check_start_after = true;
            }
        }

        let range = start_key.as_str()..;
        let iter = table.range(range)
            .map_err(|e| PyValueError::new_err(format!("Range error: {}", e)))?;
            
        let mut results = Vec::new();
        let limit = max_keys.unwrap_or(usize::MAX);
        
        for item in iter {
            if let Ok((key, value)) = item {
                let current_key = key.value();
                if !current_key.starts_with(&prefix) {
                    break;
                }
                if check_start_after && current_key == start_after.as_ref().unwrap() {
                    continue; // start_after is exclusive in S3
                }
                
                results.push((current_key.to_string(), value.value().to_string()));
                if results.len() >= limit {
                    break;
                }
            }
        }
        
        Ok(results)
    }
}

#[pymodule]
fn p2_s3_meta(_py: Python, m: &PyModule) -> PyResult<()> {
    m.add_class::<MetaEngine>()?;
    Ok(())
}
