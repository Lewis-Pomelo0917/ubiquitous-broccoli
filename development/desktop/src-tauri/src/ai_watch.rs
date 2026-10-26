use serde::{Deserialize, Serialize};
use serde_json::Value;
#[cfg(not(windows))]
use serde_json::json;
use sha2::{Digest, Sha256};
use std::fs::{self, File, OpenOptions};
use std::io::{Read, Seek, SeekFrom, Write};
use std::path::{Component, Path, PathBuf};
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::thread;
use std::time::{Duration, SystemTime, UNIX_EPOCH};
use tauri::{AppHandle, Manager};

const RESOURCE_LOCK: &[u8] = include_bytes!("../../resources/ai-watch/resource-lock.json");
const FOOTER_MAGIC: &[u8; 8] = b"CUBICAI1";
const FOOTER_SIZE: usize = 56;
const MAX_MANIFEST_SIZE: u64 = 1_048_576;
const MAX_PAYLOAD_SIZE: u64 = 1_500_000_000;
static TEMP_FILE_COUNTER: AtomicU64 = AtomicU64::new(0);

#[derive(Clone, Serialize, Deserialize)]
#[serde(default)]
pub struct WatchStatus {
    pub schema_version: u32,
    pub state: String,
    pub last_attempt_at: Option<String>,
    pub last_attempt_unix: Option<u64>,
    pub last_success_at: Option<String>,
    pub last_success_unix: Option<u64>,
    pub last_complete_at: Option<String>,
    pub last_data_change_at: Option<String>,
    pub new_items: u32,
    pub filtered_stale: u32,
    pub duplicate_items: u32,
    pub summarized_items: u32,
    pub model_skipped_items: u32,
    pub failed_sources: u32,
    pub pending_sources: u32,
    pub source_count: u32,
    pub message: String,
    /// Fields owned by the Python worker (next_due_unix, failure_kinds, ...); kept verbatim.
    #[serde(flatten)]
    pub extra: serde_json::Map<String, Value>,
}

impl Default for WatchStatus {
    fn default() -> Self {
        Self {
            schema_version: 1,
            state: "preparing".to_string(),
            last_attempt_at: None,
            last_attempt_unix: None,
            last_success_at: None,
            last_success_unix: None,
            last_complete_at: None,
            last_data_change_at: None,
            new_items: 0,
            filtered_stale: 0,
            duplicate_items: 0,
            summarized_items: 0,
            model_skipped_items: 0,
            failed_sources: 0,
            pending_sources: 0,
            source_count: 0,
            message: "正在准备本地情报组件".to_string(),
            extra: serde_json::Map::new(),
        }
    }
}

#[derive(Default)]
pub struct AiWatchState {
    running: AtomicBool,
    shutdown: AtomicBool,
    #[cfg_attr(not(windows), allow(dead_code))]
    refresh: AtomicBool,
}

#[derive(Debug, Deserialize)]
struct PayloadEntry {
    path: String,
    offset: u64,
    size: u64,
    sha256: String,
}

#[derive(Debug, Deserialize)]
struct PayloadManifest {
    format_version: u32,
    resource_lock_sha256: String,
    model_sha256: String,
    llama_archive_sha256: String,
    python_archive_sha256: String,
    entries: Vec<PayloadEntry>,
}

fn sha256_bytes(bytes: &[u8]) -> String {
    format!("{:x}", Sha256::digest(bytes))
}

fn hex_bytes(bytes: &[u8]) -> String {
    bytes.iter().map(|byte| format!("{byte:02x}")).collect()
}

fn sha256_file(path: &Path) -> Result<(u64, String), String> {
    let mut file = File::open(path).map_err(|error| format!("无法读取组件：{error}"))?;
    let mut hash = Sha256::new();
    let mut size = 0u64;
    let mut buffer = [0u8; 1024 * 1024];
    loop {
        let count = file.read(&mut buffer).map_err(|error| format!("组件读取失败：{error}"))?;
        if count == 0 {
            break;
        }
        size += count as u64;
        hash.update(&buffer[..count]);
    }
    Ok((size, format!("{:x}", hash.finalize())))
}

fn read_payload_manifest(executable: &Path) -> Result<(PayloadManifest, u64, String), String> {
    let mut file = File::open(executable).map_err(|error| format!("无法读取程序包：{error}"))?;
    let file_len = file.metadata().map_err(|error| error.to_string())?.len();
    if file_len < FOOTER_SIZE as u64 {
        return Err("程序包缺少本地组件载荷".to_string());
    }
    file.seek(SeekFrom::End(-(FOOTER_SIZE as i64)))
        .map_err(|error| error.to_string())?;
    let mut footer = [0u8; FOOTER_SIZE];
    file.read_exact(&mut footer).map_err(|error| error.to_string())?;
    if &footer[48..56] != FOOTER_MAGIC {
        return Err("程序包格式不包含完整本地组件".to_string());
    }
    let payload_offset = u64::from_le_bytes(footer[0..8].try_into().map_err(|_| "载荷偏移无效")?);
    let manifest_len = u64::from_le_bytes(footer[8..16].try_into().map_err(|_| "清单长度无效")?);
    if manifest_len == 0 || manifest_len > MAX_MANIFEST_SIZE || file_len < FOOTER_SIZE as u64 + manifest_len {
        return Err("程序包清单长度无效".to_string());
    }
    let manifest_offset = file_len - FOOTER_SIZE as u64 - manifest_len;
    if payload_offset == 0 || payload_offset > manifest_offset {
        return Err("程序包载荷区间无效".to_string());
    }
    let payload_size = manifest_offset - payload_offset;
    if payload_size > MAX_PAYLOAD_SIZE {
        return Err("程序包载荷超过安全上限".to_string());
    }
    file.seek(SeekFrom::Start(manifest_offset)).map_err(|error| error.to_string())?;
    let mut bytes = vec![0u8; manifest_len as usize];
    file.read_exact(&mut bytes).map_err(|error| error.to_string())?;
    let manifest_hash = sha256_bytes(&bytes);
    let expected_hash = hex_bytes(&footer[16..48]);
    if manifest_hash != expected_hash {
        return Err("程序包清单校验失败".to_string());
    }
    let manifest: PayloadManifest = serde_json::from_slice(&bytes).map_err(|_| "程序包清单无法解析".to_string())?;
    validate_manifest(&manifest, payload_size)?;
    Ok((manifest, payload_offset, manifest_hash))
}

fn validate_manifest(manifest: &PayloadManifest, payload_size: u64) -> Result<(), String> {
    if manifest.format_version != 1 || manifest.entries.is_empty() || manifest.entries.len() > 256 {
        return Err("程序包版本或组件数量无效".to_string());
    }
    let lock: Value = serde_json::from_slice(RESOURCE_LOCK).map_err(|_| "内置资源锁定清单无效".to_string())?;
    if manifest.resource_lock_sha256 != sha256_bytes(RESOURCE_LOCK)
        || manifest.model_sha256 != lock["model"]["sha256"].as_str().unwrap_or("")
        || manifest.llama_archive_sha256 != lock["inference_runtime"]["sha256"].as_str().unwrap_or("")
        || manifest.python_archive_sha256 != lock["python_runtime"]["sha256"].as_str().unwrap_or("")
    {
        return Err("程序包组件版本与应用锁定清单不一致".to_string());
    }

    let model_entry = "model/Qwen3-0.6B-Q4_0.gguf";
    let mut names = std::collections::HashSet::new();
    let mut ranges = Vec::with_capacity(manifest.entries.len());
    let mut has_model = false;
    let mut required = [
        "python/python.exe",
        "worker/ai_radar/watch.py",
        "worker/ai_radar/sources.json",
        "llama/llama-completion.exe",
        "llama/msvcp140.dll",
        "llama/vcruntime140.dll",
        "llama/vcruntime140_1.dll",
        model_entry,
        "schema/summary-schema.json",
        "feed/initial-ai-intelligence.json",
    ]
    .into_iter()
    .collect::<std::collections::HashSet<_>>();

    for entry in &manifest.entries {
        validate_entry_path(&entry.path)?;
        if !names.insert(entry.path.as_str()) || entry.size == 0 || entry.sha256.len() != 64 {
            return Err("程序包包含重复或无效组件".to_string());
        }
        let end = entry.offset.checked_add(entry.size).ok_or_else(|| "程序包组件区间溢出".to_string())?;
        if end > payload_size {
            return Err("程序包组件越过载荷范围".to_string());
        }
        ranges.push((entry.offset, end));
        if entry.path == model_entry {
            has_model = true;
            if entry.sha256 != manifest.model_sha256
                || entry.size != lock["model"]["size_bytes"].as_u64().unwrap_or(0)
            {
                return Err("内置模型与锁定模型不一致".to_string());
            }
        }
        required.remove(entry.path.as_str());
    }
    if !has_model || !required.is_empty() {
        return Err("程序包缺少必要推理或采集组件".to_string());
    }
    let msvc_files = lock["msvc_runtime"]["files"].as_array().ok_or_else(|| "MSVC runtime 锁定清单无效".to_string())?;
    for runtime in msvc_files {
        let name = runtime["name"].as_str().ok_or_else(|| "MSVC runtime 文件名无效".to_string())?;
        let path = format!("llama/{name}");
        let entry = manifest.entries.iter().find(|entry| entry.path == path).ok_or_else(|| "程序包缺少锁定的 MSVC runtime".to_string())?;
        if entry.sha256 != runtime["sha256"].as_str().unwrap_or("")
            || entry.size != runtime["size_bytes"].as_u64().unwrap_or(0)
        {
            return Err("内置 MSVC runtime 与资源锁定清单不一致".to_string());
        }
    }
    ranges.sort_unstable();
    if ranges.windows(2).any(|pair| pair[0].1 > pair[1].0) {
        return Err("程序包组件区间重叠".to_string());
    }
    Ok(())
}

fn validate_entry_path(name: &str) -> Result<(), String> {
    if name.is_empty()
        || name.contains('\\')
        || name.contains(':')
        || name.starts_with('/')
        || name.split('/').any(|part| part.is_empty() || part == "." || part == "..")
        || !name.bytes().all(|byte| byte.is_ascii_alphanumeric() || b"._/-".contains(&byte))
    {
        return Err("程序包包含不安全的组件路径".to_string());
    }
    if Path::new(name).components().any(|part| !matches!(part, Component::Normal(_))) {
        return Err("程序包包含不安全的组件路径".to_string());
    }
    Ok(())
}

fn entry_path(root: &Path, name: &str) -> Result<PathBuf, String> {
    validate_entry_path(name)?;
    Ok(name.split('/').fold(root.to_path_buf(), |path, part| path.join(part)))
}

fn manifest_file_matches(root: &Path, manifest_hash: &str) -> bool {
    fs::read(root.join("manifest.json"))
        .map(|bytes| sha256_bytes(&bytes) == manifest_hash)
        .unwrap_or(false)
}

fn verify_entry(root: &Path, entry: &PayloadEntry) -> bool {
    let path = match entry_path(root, &entry.path) {
        Ok(value) => value,
        Err(_) => return false,
    };
    match fs::symlink_metadata(&path) {
        Ok(metadata) if metadata.file_type().is_file() => {
            metadata.len() == entry.size
                && sha256_file(&path).map(|(_, hash)| hash == entry.sha256).unwrap_or(false)
        }
        _ => false,
    }
}

fn verify_runtime(root: &Path, manifest: &PayloadManifest, manifest_hash: &str) -> bool {
    let marker = fs::read_to_string(root.join(".cubic-ai-runtime"));
    if marker.map(|value| value.trim() != manifest_hash).unwrap_or(true)
        || !manifest_file_matches(root, manifest_hash)
    {
        return false;
    }
    manifest.entries.iter().all(|entry| verify_entry(root, entry))
}

fn write_entry(executable: &Path, payload_offset: u64, entry: &PayloadEntry, destination: &Path) -> Result<(), String> {
    let parent = destination.parent().ok_or_else(|| "组件路径无父目录".to_string())?;
    fs::create_dir_all(parent).map_err(|error| format!("无法准备本地组件目录：{error}"))?;
    let mut source = File::open(executable).map_err(|error| format!("无法读取程序包：{error}"))?;
    source
        .seek(SeekFrom::Start(payload_offset + entry.offset))
        .map_err(|error| error.to_string())?;
    let mut target = OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(destination)
        .map_err(|error| format!("无法释放本地组件：{error}"))?;
    let mut hasher = Sha256::new();
    let mut remaining = entry.size;
    let mut buffer = [0u8; 1024 * 1024];
    while remaining > 0 {
        let count = remaining.min(buffer.len() as u64) as usize;
        source.read_exact(&mut buffer[..count]).map_err(|error| format!("组件载荷读取失败：{error}"))?;
        target.write_all(&buffer[..count]).map_err(|error| format!("组件写入失败：{error}"))?;
        hasher.update(&buffer[..count]);
        remaining -= count as u64;
    }
    target.sync_all().map_err(|error| format!("组件落盘失败：{error}"))?;
    if format!("{:x}", hasher.finalize()) != entry.sha256 {
        return Err("本地组件 SHA-256 校验失败".to_string());
    }
    Ok(())
}

fn extract_entry(executable: &Path, payload_offset: u64, entry: &PayloadEntry, root: &Path) -> Result<(), String> {
    write_entry(executable, payload_offset, entry, &entry_path(root, &entry.path)?)
}

fn repair_runtime_files(
    executable: &Path,
    payload_offset: u64,
    root: &Path,
    manifest: &PayloadManifest,
    manifest_hash: &str,
) -> Result<bool, String> {
    if !root.is_dir() || !manifest_file_matches(root, manifest_hash) {
        return Ok(false);
    }
    for entry in &manifest.entries {
        let destination = entry_path(root, &entry.path)?;
        let parent = destination.parent().ok_or_else(|| "组件路径无父目录".to_string())?;
        if parent.is_dir() {
            for candidate in fs::read_dir(parent).map_err(|error| error.to_string())?.flatten() {
                let name = candidate.file_name().to_string_lossy().into_owned();
                if name.starts_with(".cubic-ai-repair-") && name.ends_with(".tmp") && candidate.path().is_file() {
                    let _ = fs::remove_file(candidate.path());
                }
            }
        }
        if verify_entry(root, entry) {
            continue;
        }
        if let Ok(metadata) = fs::symlink_metadata(&destination) {
            if metadata.file_type().is_symlink() || !metadata.is_file() {
                return Err("本地组件路径包含非普通文件；为保护目录内容，未覆盖".to_string());
            }
        }
        let temp = parent.join(format!(
            ".cubic-ai-repair-{}-{}.tmp",
            std::process::id(),
            TEMP_FILE_COUNTER.fetch_add(1, Ordering::Relaxed)
        ));
        write_entry(executable, payload_offset, entry, &temp)?;
        if let Err(error) = atomic_move(&temp, &destination, true) {
            let _ = fs::remove_file(&temp);
            return Err(format!("无法安全修复本地组件：{error}"));
        }
    }
    let marker = root.join(".cubic-ai-runtime");
    atomic_write(&marker, manifest_hash.as_bytes(), false)?;
    Ok(verify_runtime(root, manifest, manifest_hash))
}

fn cleanup_staging(runtime_root: &Path) {
    if let Ok(entries) = fs::read_dir(runtime_root) {
        for entry in entries.flatten() {
            let name = entry.file_name().to_string_lossy().into_owned();
            if name.starts_with(".staging-") && entry.path().is_dir() {
                let _ = fs::remove_dir_all(entry.path());
            }
        }
    }
}

fn cleanup_old_runtime_versions(runtime_root: &Path, current: &Path) {
    if let Ok(entries) = fs::read_dir(runtime_root) {
        for entry in entries.flatten() {
            let path = entry.path();
            if path == current || !path.is_dir() {
                continue;
            }
            let marker = match fs::read_to_string(path.join(".cubic-ai-runtime")) {
                Ok(value) => value.trim().to_string(),
                Err(_) => continue,
            };
            if marker.len() == 64 && sha256_file(&path.join("manifest.json")).map(|(_, hash)| hash == marker).unwrap_or(false) {
                let _ = fs::remove_dir_all(path);
            }
        }
    }
}

fn ensure_runtime(executable: &Path, app_data: &Path) -> Result<PathBuf, String> {
    #[cfg(windows)]
    let _install_lock = RuntimeInstallLock::acquire()?;
    let (manifest, payload_offset, manifest_hash) = read_payload_manifest(executable)?;
    let runtime_root = app_data.join("ai-runtime");
    fs::create_dir_all(&runtime_root).map_err(|error| format!("无法准备本地模型目录：{error}"))?;
    cleanup_staging(&runtime_root);
    let target = runtime_root.join(&manifest_hash);
    let backup = runtime_root.join(format!(".backup-{manifest_hash}"));

    if verify_runtime(&target, &manifest, &manifest_hash) {
        if backup.exists() && fs::read_to_string(backup.join(".cubic-ai-runtime")).map(|value| value.trim() == manifest_hash).unwrap_or(false) {
            let _ = fs::remove_dir_all(&backup);
        }
        cleanup_old_runtime_versions(&runtime_root, &target);
        return Ok(target);
    }
    if target.exists() {
        match repair_runtime_files(executable, payload_offset, &target, &manifest, &manifest_hash) {
            Ok(true) => {
                if backup.exists() && fs::read_to_string(backup.join(".cubic-ai-runtime")).map(|value| value.trim() == manifest_hash).unwrap_or(false) {
                    let _ = fs::remove_dir_all(&backup);
                }
                cleanup_old_runtime_versions(&runtime_root, &target);
                return Ok(target);
            }
            Ok(false) => {}
            Err(error) => return Err(format!("本地组件修复失败：{error}")),
        }
    }
    if !target.exists() && verify_runtime(&backup, &manifest, &manifest_hash) {
        fs::rename(&backup, &target).map_err(|error| format!("无法恢复上次完整组件：{error}"))?;
        return Ok(target);
    }

    let staging = runtime_root.join(format!(".staging-{manifest_hash}-{}", std::process::id()));
    if staging.exists() {
        fs::remove_dir_all(&staging).map_err(|error| format!("无法清理未完成的组件释放：{error}"))?;
    }
    fs::create_dir(&staging).map_err(|error| format!("无法创建组件暂存目录：{error}"))?;
    let extract_result = (|| {
        for entry in &manifest.entries {
            extract_entry(executable, payload_offset, entry, &staging)?;
        }
        fs::write(staging.join("manifest.json"), {
            let mut file = File::open(executable).map_err(|error| error.to_string())?;
            let len = file.metadata().map_err(|error| error.to_string())?.len();
            file.seek(SeekFrom::End(-(FOOTER_SIZE as i64))).map_err(|error| error.to_string())?;
            let mut footer = [0u8; FOOTER_SIZE];
            file.read_exact(&mut footer).map_err(|error| error.to_string())?;
            let manifest_len = u64::from_le_bytes(footer[8..16].try_into().map_err(|_| "清单长度无效")?) as usize;
            let offset = len as usize - FOOTER_SIZE - manifest_len;
            file.seek(SeekFrom::Start(offset as u64)).map_err(|error| error.to_string())?;
            let mut bytes = vec![0u8; manifest_len];
            file.read_exact(&mut bytes).map_err(|error| error.to_string())?;
            bytes
        }).map_err(|error| format!("无法保存本地组件清单：{error}"))?;
        fs::write(staging.join(".cubic-ai-runtime"), &manifest_hash)
            .map_err(|error| format!("无法记录组件版本：{error}"))?;
        if !verify_runtime(&staging, &manifest, &manifest_hash) {
            return Err("本地组件完整性检查失败".to_string());
        }
        Ok::<(), String>(())
    })();

    if let Err(error) = extract_result {
        let _ = fs::remove_dir_all(&staging);
        return Err(error);
    }

    let mut moved_target = false;
    if target.exists() {
        let marker = fs::read_to_string(target.join(".cubic-ai-runtime"))
            .map_err(|_| "已有运行组件目录无法识别；为保护其中数据，未覆盖".to_string())?;
        if marker.trim() != manifest_hash {
            let _ = fs::remove_dir_all(&staging);
            return Err("已有运行组件目录版本不匹配；为保护其中数据，未覆盖".to_string());
        }
        if backup.exists() {
            if verify_runtime(&backup, &manifest, &manifest_hash) {
                fs::remove_dir_all(&target).map_err(|error| format!("无法替换损坏组件：{error}"))?;
                fs::rename(&backup, &target).map_err(|error| format!("无法恢复组件备份：{error}"))?;
                let _ = fs::remove_dir_all(&staging);
                if verify_runtime(&target, &manifest, &manifest_hash) {
                    return Ok(target);
                }
                return Err("组件备份恢复后仍未通过校验".to_string());
            }
            let backup_marker = fs::read_to_string(backup.join(".cubic-ai-runtime"))
                .map_err(|_| "组件备份路径无法识别；为保护其中数据，未覆盖".to_string())?;
            if backup_marker.trim() != manifest_hash {
                let _ = fs::remove_dir_all(&staging);
                return Err("组件备份版本不匹配；为保护其中数据，未覆盖".to_string());
            }
            fs::remove_dir_all(&backup).map_err(|error| format!("无法清理无效组件备份：{error}"))?;
        }
        fs::rename(&target, &backup).map_err(|error| format!("无法切换损坏组件：{error}"))?;
        moved_target = true;
    }

    if let Err(error) = fs::rename(&staging, &target) {
        if moved_target {
            let _ = fs::rename(&backup, &target);
        }
        let _ = fs::remove_dir_all(&staging);
        return Err(format!("无法启用完整本地组件：{error}"));
    }
    if moved_target {
        let _ = fs::remove_dir_all(&backup);
    }
    if backup.exists() && fs::read_to_string(backup.join(".cubic-ai-runtime")).map(|value| value.trim() == manifest_hash).unwrap_or(false) {
        let _ = fs::remove_dir_all(&backup);
    }
    cleanup_old_runtime_versions(&runtime_root, &target);
    Ok(target)
}

fn atomic_write(path: &Path, bytes: &[u8], no_clobber: bool) -> Result<(), String> {
    let parent = path.parent().ok_or_else(|| "数据文件路径无效".to_string())?;
    fs::create_dir_all(parent).map_err(|error| format!("无法准备数据目录：{error}"))?;
    if no_clobber && path.exists() {
        return Ok(());
    }
    let file_name = path.file_name().unwrap_or_default().to_string_lossy();
    for _ in 0..32 {
        let temp_path = parent.join(format!(
            ".{file_name}.{}.{}.tmp",
            std::process::id(),
            TEMP_FILE_COUNTER.fetch_add(1, Ordering::Relaxed)
        ));
        let mut temp = match OpenOptions::new().write(true).create_new(true).open(&temp_path) {
            Ok(file) => file,
            Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => continue,
            Err(error) => return Err(format!("无法创建临时文件：{error}")),
        };
        let write_result = temp
            .write_all(bytes)
            .and_then(|_| temp.sync_all())
            .map_err(|error| format!("临时文件写入失败：{error}"));
        drop(temp);
        if let Err(error) = write_result {
            let _ = fs::remove_file(&temp_path);
            return Err(error);
        }
        match atomic_move(&temp_path, path, !no_clobber) {
            Ok(()) => return Ok(()),
            Err(_) if no_clobber && path.exists() => {
                let _ = fs::remove_file(&temp_path);
                return Ok(());
            }
            Err(error) => {
                let _ = fs::remove_file(&temp_path);
                return Err(format!("原子写入失败：{error}"));
            }
        }
    }
    Err("无法分配唯一临时文件名".to_string())
}

#[cfg(windows)]
fn atomic_move(source: &Path, destination: &Path, replace: bool) -> std::io::Result<()> {
    use std::os::windows::ffi::OsStrExt;
    use windows_sys::Win32::Storage::FileSystem::{MoveFileExW, MOVEFILE_REPLACE_EXISTING, MOVEFILE_WRITE_THROUGH};
    let source: Vec<u16> = source.as_os_str().encode_wide().chain(std::iter::once(0)).collect();
    let destination: Vec<u16> = destination.as_os_str().encode_wide().chain(std::iter::once(0)).collect();
    let flags = MOVEFILE_WRITE_THROUGH | if replace { MOVEFILE_REPLACE_EXISTING } else { 0 };
    let ok = unsafe { MoveFileExW(source.as_ptr(), destination.as_ptr(), flags) };
    if ok == 0 {
        Err(std::io::Error::last_os_error())
    } else {
        Ok(())
    }
}

#[cfg(not(windows))]
fn atomic_move(source: &Path, destination: &Path, replace: bool) -> std::io::Result<()> {
    if !replace && destination.exists() {
        return Err(std::io::Error::new(std::io::ErrorKind::AlreadyExists, "destination exists"));
    }
    fs::rename(source, destination)
}

fn validate_feed_bytes(bytes: &[u8]) -> Result<(), String> {
    let feed: Value = serde_json::from_slice(bytes).map_err(|_| "初始情报文件无法解析".to_string())?;
    if feed["version"] != 1 || !feed["events"].is_array() {
        return Err("初始情报文件结构无效".to_string());
    }
    Ok(())
}

fn seed_feed_if_missing(runtime: &Path, app_data: &Path) -> Result<(), String> {
    let feed_path = app_data.join("ai-intelligence.json");
    if feed_path.exists() {
        let bytes = fs::read(&feed_path).map_err(|error| format!("无法读取本机情报：{error}"))?;
        validate_feed_bytes(&bytes)?;
        return Ok(());
    }
    let seed = fs::read(runtime.join("feed").join("initial-ai-intelligence.json"))
        .map_err(|error| format!("完整包缺少初始情报：{error}"))?;
    validate_feed_bytes(&seed)?;
    atomic_write(&feed_path, &seed, true)
}

fn app_data_dir(app: &AppHandle) -> Result<PathBuf, String> {
    app.path().app_local_data_dir().map_err(|error| format!("无法定位本机应用数据目录：{error}"))
}

fn status_path(app_data: &Path) -> PathBuf {
    app_data.join("ai-watch-status.json")
}

fn load_status(path: &Path) -> Result<WatchStatus, String> {
    if !path.exists() {
        return Ok(WatchStatus::default());
    }
    let bytes = fs::read(path).map_err(|error| format!("无法读取更新状态：{error}"))?;
    serde_json::from_slice(&bytes).map_err(|_| "更新状态文件格式无效".to_string())
}

fn save_status(path: &Path, status: &WatchStatus) -> Result<(), String> {
    let bytes = serde_json::to_vec_pretty(status).map_err(|error| error.to_string())?;
    atomic_write(path, &bytes, false)
}

fn current_unix() -> u64 {
    SystemTime::now().duration_since(UNIX_EPOCH).unwrap_or_default().as_secs()
}

fn should_run(status: &WatchStatus) -> bool {
    if status.state == "interrupted" || status.state == "preparing" {
        return true;
    }
    let now = current_unix();
    // The worker records when the next update is due: tomorrow morning after a good run,
    // sooner (with backoff) after trouble. Ignore values that are implausibly far away.
    if let Some(due) = status.extra.get("next_due_unix").and_then(Value::as_u64) {
        if due <= now.saturating_add(7 * 86_400) {
            return now >= due;
        }
    }
    if status.state == "failed" || status.state == "partial" {
        return status.last_attempt_unix.map(|value| now.saturating_sub(value) >= 3600).unwrap_or(true);
    }
    status.last_success_unix.map(|value| now.saturating_sub(value) >= 86_400).unwrap_or(true)
}

fn prepare_status(previous: &WatchStatus, state: &str, message: &str) -> WatchStatus {
    let mut next = previous.clone();
    next.schema_version = 1;
    next.state = state.to_string();
    next.message = message.to_string();
    next
}

pub fn read_ai_feed(app: &AppHandle) -> Result<String, String> {
    let feed = app_data_dir(app)?.join("ai-intelligence.json");
    match fs::read_to_string(&feed) {
        Ok(raw) => {
            validate_feed_bytes(raw.as_bytes())?;
            Ok(raw)
        }
        Err(error) => {
            #[cfg(not(windows))]
            {
                let executable = std::env::current_exe().map_err(|_| "无法定位应用程序".to_string())?;
                if let Some(path) = executable.parent().map(|parent| parent.join("ai-intelligence.json")) {
                    if let Ok(raw) = fs::read_to_string(path) {
                        validate_feed_bytes(raw.as_bytes())?;
                        return Ok(raw);
                    }
                }
            }
            Err(format!("本机 AI 情报不可用：{error}"))
        }
    }
}

pub fn read_ai_status(app: &AppHandle) -> Result<String, String> {
    #[cfg(not(windows))]
    {
        return Ok(json!({
            "schema_version": 1,
            "state": "unsupported",
            "message": "本地模型自动更新当前仅支持 Windows x64"
        })
        .to_string());
    }
    #[cfg(windows)]
    {
        let path = status_path(&app_data_dir(app)?);
        let status = load_status(&path)?;
        serde_json::to_string(&status).map_err(|error| error.to_string())
    }
}

/// Ask the scheduler to check the sources now (the user pressed the refresh button).
pub fn request_refresh(app: &AppHandle) -> Result<String, String> {
    #[cfg(not(windows))]
    {
        let _ = app;
        return Ok(json!({ "accepted": false, "reason": "unsupported" }).to_string());
    }
    #[cfg(windows)]
    {
        let state = app.state::<AiWatchState>();
        if state.running.load(Ordering::SeqCst) {
            return Ok(r#"{"accepted":false,"reason":"running"}"#.to_string());
        }
        state.refresh.store(true, Ordering::SeqCst);
        Ok(r#"{"accepted":true}"#.to_string())
    }
}

pub fn start(app: AppHandle) -> Result<(), String> {
    #[cfg(windows)]
    {
        thread::Builder::new()
            .name("cubic-ai-watch".to_string())
            .spawn(move || scheduler_loop(app))
            .map_err(|error| format!("无法启动本机更新调度：{error}"))?;
    }
    #[cfg(not(windows))]
    {
        let _ = app;
    }
    Ok(())
}

pub fn shutdown(app: &AppHandle) {
    let state = app.state::<AiWatchState>();
    state.shutdown.store(true, Ordering::SeqCst);
}

#[cfg(windows)]
fn scheduler_loop(app: AppHandle) {
    let state_path;
    let data_dir;
    match app_data_dir(&app) {
        Ok(path) => {
            data_dir = path;
            state_path = status_path(&data_dir);
        }
        Err(_) => return,
    }
    if fs::create_dir_all(&data_dir).is_err() {
        return;
    }
    let previous = load_status(&state_path).unwrap_or_default();
    let interrupted = previous.state == "updating";
    let mut preparing = prepare_status(&previous, "preparing", "正在校验并准备包内本地组件");
    let _ = save_status(&state_path, &preparing);

    let executable = match std::env::current_exe() {
        Ok(path) => path,
        Err(_) => {
            preparing.state = "failed".to_string();
            preparing.message = "无法定位完整应用程序".to_string();
            let _ = save_status(&state_path, &preparing);
            return;
        }
    };
    let runtime = match ensure_runtime(&executable, &data_dir).and_then(|path| {
        seed_feed_if_missing(&path, &data_dir)?;
        Ok(path)
    }) {
        Ok(path) => path,
        Err(error) => {
            preparing.state = "failed".to_string();
            preparing.message = format!("本地组件准备失败：{error}");
            let _ = save_status(&state_path, &preparing);
            return;
        }
    };
    let mut ready = previous;
    if interrupted {
        ready.state = "interrupted".to_string();
        ready.message = "检测到上次更新中断；已有情报已保留，将重新检查".to_string();
        let _ = save_status(&state_path, &ready);
    } else {
        let _ = save_status(&state_path, &ready);
    }

    loop {
        if app.state::<AiWatchState>().shutdown.load(Ordering::SeqCst) {
            break;
        }
        let status = load_status(&state_path).unwrap_or_default();
        let forced = app.state::<AiWatchState>().refresh.swap(false, Ordering::SeqCst);
        if forced || should_run(&status) {
            let watch_state = app.state::<AiWatchState>();
            if watch_state
                .running
                .compare_exchange(false, true, Ordering::SeqCst, Ordering::SeqCst)
                .is_ok()
            {
                let mut attempt = prepare_status(&status, "updating", "正在联网检查公开来源");
                attempt.last_attempt_at = None;
                attempt.last_attempt_unix = Some(current_unix());
                let _ = save_status(&state_path, &attempt);
                let outcome = run_worker(&runtime, &data_dir);
                watch_state.running.store(false, Ordering::SeqCst);
                let mut final_status = load_status(&state_path).unwrap_or_else(|_| {
                    let mut failed = attempt;
                    failed.state = "failed".to_string();
                    failed.message = "更新状态无法读取；最近成功情报已保留".to_string();
                    failed
                });
                // The worker writes its own final state. If it never got that far (could not start,
                // crashed, or was stopped by the watchdog) say so instead of leaving "updating" behind.
                if final_status.state == "updating" || final_status.state == "interrupted" {
                    mark_worker_failure(&mut final_status, outcome.err());
                }
                let _ = save_status(&state_path, &final_status);
            }
        }
        for _ in 0..900 {
            let watch_state = app.state::<AiWatchState>();
            if watch_state.shutdown.load(Ordering::SeqCst) || watch_state.refresh.load(Ordering::SeqCst) {
                break;
            }
            thread::sleep(Duration::from_secs(1));
        }
    }
}

/// Record that the worker ended without publishing a result, and back off before the next try.
fn mark_worker_failure(status: &mut WatchStatus, reason: Option<String>) {
    let failures = status
        .extra
        .get("consecutive_failures")
        .and_then(Value::as_u64)
        .unwrap_or(0)
        .saturating_add(1);
    let delay = (1800u64.saturating_mul(1u64 << failures.saturating_sub(1).min(4))).min(6 * 3600);
    status.state = "failed".to_string();
    status.message = reason.unwrap_or_else(|| "本机更新异常结束；最近成功情报已保留".to_string());
    status.extra.insert("consecutive_failures".to_string(), Value::from(failures));
    status
        .extra
        .insert("next_due_unix".to_string(), Value::from(current_unix().saturating_add(delay)));
}

/// Longest a single update may run: collection budget (20 min) plus the local model budget and slack.
const WORKER_TIMEOUT: Duration = Duration::from_secs(35 * 60);

#[cfg(windows)]
fn run_worker(runtime: &Path, data_dir: &Path) -> Result<(), String> {
    let python = runtime.join("python").join("python.exe");
    let runner = Path::new("llama").join("llama-completion.exe");
    let model = Path::new("model").join("Qwen3-0.6B-Q4_0.gguf");
    let schema = Path::new("schema").join("summary-schema.json");
    let sources = Path::new("worker").join("ai_radar").join("sources.json");
    let feed = data_dir.join("ai-intelligence.json");
    let status = status_path(data_dir);
    let state_dir = data_dir.join("ai-watch");
    fs::create_dir_all(&state_dir).map_err(|error| format!("无法准备更新状态目录：{error}"))?;
    let mut command = std::process::Command::new(python);
    command
        .arg("-m")
        .arg("ai_radar.watch")
        .arg("--data-dir")
        .arg(data_dir)
        .arg("--feed")
        .arg(feed)
        .arg("--status")
        .arg(&status)
        .arg("--db")
        .arg(state_dir.join("state.sqlite3"))
        .arg("--runner")
        .arg(runner)
        .arg("--model")
        .arg(model)
        .arg("--schema")
        .arg(schema)
        .arg("--sources")
        .arg(sources)
        .arg("--run-timeout")
        .arg("1200")
        .arg("--source-timeout")
        .arg("30")
        .current_dir(runtime)
        .env("PYTHONDONTWRITEBYTECODE", "1")
        .stdout(std::process::Stdio::piped())
        .stderr(std::process::Stdio::piped());
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        command.creation_flags(0x08000000);
    }
    let job = KillOnCloseJob::new()?;
    let child = command.spawn().map_err(|error| format!("本机采集组件无法启动：{error}"))?;
    if let Err(error) = job.assign(&child) {
        let mut child = child;
        let _ = child.kill();
        let _ = child.wait();
        return Err(error);
    }
    // Wait on a helper thread so a hung worker cannot block the scheduler forever.
    let (sender, receiver) = std::sync::mpsc::channel();
    thread::spawn(move || {
        let _ = sender.send(child.wait_with_output());
    });
    let output = match receiver.recv_timeout(WORKER_TIMEOUT) {
        Ok(result) => result.map_err(|error| format!("本机采集组件异常结束：{error}"))?,
        Err(_) => {
            drop(job); // closing the job terminates the worker and any model process it started
            return Err("本机更新超时，已终止；最近成功情报已保留".to_string());
        }
    };
    if !output.status.success() {
        let state = load_status(&status).ok();
        if state.as_ref().map(|value| value.state.as_str()) != Some("failed") {
            return Err("本机采集或模型校验失败；最近成功情报已保留".to_string());
        }
    }
    Ok(())
}

#[cfg(not(windows))]
fn run_worker(_runtime: &Path, _data_dir: &Path) -> Result<(), String> {
    Err("本地模型更新当前仅支持 Windows x64".to_string())
}

#[cfg(windows)]
struct KillOnCloseJob(windows_sys::Win32::Foundation::HANDLE);

#[cfg(windows)]
struct RuntimeInstallLock(windows_sys::Win32::Foundation::HANDLE);

#[cfg(windows)]
impl RuntimeInstallLock {
    fn acquire() -> Result<Self, String> {
        use windows_sys::Win32::Foundation::{CloseHandle, WAIT_ABANDONED, WAIT_OBJECT_0};
        use windows_sys::Win32::System::Threading::{CreateMutexW, WaitForSingleObject};
        let name: Vec<u16> = "Local\\com.tradedesk.app.ai-runtime".encode_utf16().chain(std::iter::once(0)).collect();
        let handle = unsafe { CreateMutexW(std::ptr::null(), 0, name.as_ptr()) };
        if handle.is_null() {
            return Err("无法建立本地组件互斥锁".to_string());
        }
        let wait = unsafe { WaitForSingleObject(handle, 120_000) };
        if wait == WAIT_OBJECT_0 || wait == WAIT_ABANDONED {
            Ok(Self(handle))
        } else {
            unsafe { CloseHandle(handle) };
            Err("另一实例正在准备本地组件；本次未覆盖现有数据".to_string())
        }
    }
}

#[cfg(windows)]
impl Drop for RuntimeInstallLock {
    fn drop(&mut self) {
        use windows_sys::Win32::Foundation::CloseHandle;
        use windows_sys::Win32::System::Threading::ReleaseMutex;
        unsafe {
            ReleaseMutex(self.0);
            CloseHandle(self.0);
        }
    }
}

#[cfg(windows)]
impl KillOnCloseJob {
    fn new() -> Result<Self, String> {
        use windows_sys::Win32::Foundation::CloseHandle;
        use windows_sys::Win32::System::JobObjects::{
            CreateJobObjectW, SetInformationJobObject, JobObjectExtendedLimitInformation,
            JOBOBJECT_EXTENDED_LIMIT_INFORMATION, JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
        };
        let handle = unsafe { CreateJobObjectW(std::ptr::null(), std::ptr::null()) };
        if handle.is_null() {
            return Err("无法创建更新进程生命周期保护".to_string());
        }
        let mut limits: JOBOBJECT_EXTENDED_LIMIT_INFORMATION = unsafe { std::mem::zeroed() };
        limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
        let ok = unsafe {
            SetInformationJobObject(
                handle,
                JobObjectExtendedLimitInformation,
                &limits as *const _ as *const _,
                std::mem::size_of::<JOBOBJECT_EXTENDED_LIMIT_INFORMATION>() as u32,
            )
        };
        if ok == 0 {
            unsafe { CloseHandle(handle) };
            return Err("无法设置更新进程退出保护".to_string());
        }
        Ok(Self(handle))
    }

    fn assign(&self, child: &std::process::Child) -> Result<(), String> {
        use std::os::windows::io::AsRawHandle;
        use windows_sys::Win32::System::JobObjects::AssignProcessToJobObject;
        let ok = unsafe { AssignProcessToJobObject(self.0, child.as_raw_handle() as _) };
        if ok == 0 {
            return Err("无法将采集进程绑定到应用生命周期".to_string());
        }
        Ok(())
    }
}

#[cfg(windows)]
impl Drop for KillOnCloseJob {
    fn drop(&mut self) {
        use windows_sys::Win32::Foundation::CloseHandle;
        unsafe { CloseHandle(self.0) };
    }
}

#[cfg(test)]
mod schedule_tests {
    use super::*;

    fn status_from(json: &str) -> WatchStatus {
        serde_json::from_str(json).expect("status parses")
    }

    #[test]
    fn worker_fields_survive_a_rust_rewrite() {
        let raw = r#"{"schema_version":1,"state":"partial","message":"x","failed_sources":3,
            "failure_kinds":{"超时":2},"failed_samples":[{"source_id":"a","kind":"超时","message":"m"}],
            "next_due_unix":1900000000,"consecutive_failures":0,"model_state":"ok","new_items":4}"#;
        let status = status_from(raw);
        assert_eq!(status.failed_sources, 3);
        assert_eq!(status.new_items, 4);
        let again: Value = serde_json::from_str(&serde_json::to_string(&status).unwrap()).unwrap();
        assert_eq!(again["failure_kinds"]["超时"], 2);
        assert_eq!(again["failed_samples"][0]["source_id"], "a");
        assert_eq!(again["next_due_unix"], 1900000000u64);
        assert_eq!(again["model_state"], "ok");
        assert_eq!(again["state"], "partial");
        assert!(again.get("extra").is_none(), "the flattened map must not appear as a field");
    }

    #[test]
    fn missing_fields_fall_back_to_defaults() {
        let status = status_from(r#"{"state":"updated"}"#);
        assert_eq!(status.schema_version, 1);
        assert_eq!(status.state, "updated");
        assert!(status.extra.is_empty());
    }

    #[test]
    fn the_workers_schedule_decides_when_to_run() {
        let now = current_unix();
        let mut status = status_from(r#"{"state":"updated"}"#);
        status.last_success_unix = Some(now);
        status.extra.insert("next_due_unix".into(), Value::from(now + 3600));
        assert!(!should_run(&status), "not due yet");
        status.extra.insert("next_due_unix".into(), Value::from(now - 1));
        assert!(should_run(&status), "due");
        status.extra.insert("next_due_unix".into(), Value::from(now + 30 * 86_400));
        assert!(!should_run(&status), "an implausible schedule falls back to the 24 h rule, and the last success is fresh");
        status.last_success_unix = Some(now - 90_000);
        assert!(should_run(&status));
    }

    #[test]
    fn interrupted_or_preparing_always_runs_and_old_statuses_keep_the_fixed_rules() {
        let now = current_unix();
        for state in ["interrupted", "preparing"] {
            let mut status = status_from(r#"{}"#);
            status.state = state.into();
            assert!(should_run(&status));
        }
        let mut failed = status_from(r#"{"state":"failed"}"#);
        failed.last_attempt_unix = Some(now - 600);
        assert!(!should_run(&failed));
        failed.last_attempt_unix = Some(now - 4000);
        assert!(should_run(&failed));
    }

    #[test]
    fn a_worker_that_never_finished_is_reported_with_growing_backoff() {
        let mut status = status_from(r#"{"state":"updating"}"#);
        let before = current_unix();
        let mut delays = Vec::new();
        for _ in 0..7 {
            mark_worker_failure(&mut status, Some("本机采集组件无法启动".to_string()));
            assert_eq!(status.state, "failed");
            assert_eq!(status.message, "本机采集组件无法启动");
            delays.push(status.extra["next_due_unix"].as_u64().unwrap() - before);
        }
        let near = |value: u64, expected: u64| value >= expected && value <= expected + 5;
        assert!(near(delays[0], 1800) && near(delays[1], 3600) && near(delays[2], 7200) && near(delays[3], 14400));
        assert!(near(delays[4], 21600) && near(delays[5], 21600) && near(delays[6], 21600));
        assert_eq!(status.extra["consecutive_failures"], 7);
        mark_worker_failure(&mut status, None);
        assert!(status.message.contains("异常结束"));
    }
}

#[cfg(all(test, windows))]
mod tests {
    use super::*;
    use std::process::Command;
    use std::sync::{Arc, Barrier};

    struct RemoveTestDirectory(PathBuf);

    impl Drop for RemoveTestDirectory {
        fn drop(&mut self) {
            let _ = fs::remove_dir_all(&self.0);
        }
    }

    #[test]
    fn update_guard_prevents_reentry() {
        let state = AiWatchState::default();
        assert!(state.running.compare_exchange(false, true, Ordering::SeqCst, Ordering::SeqCst).is_ok());
        assert!(state.running.compare_exchange(false, true, Ordering::SeqCst, Ordering::SeqCst).is_err());
        state.running.store(false, Ordering::SeqCst);
        assert!(state.running.compare_exchange(false, true, Ordering::SeqCst, Ordering::SeqCst).is_ok());
    }

    #[test]
    fn single_file_extracts_offline_and_repairs_only_managed_cache_when_requested() -> Result<(), String> {
        let Some(executable) = std::env::var_os("CUBIC_AI_TEST_EXE").map(PathBuf::from) else {
            return Ok(());
        };
        let Some(data_dir) = std::env::var_os("CUBIC_AI_TEST_DATA_DIR").map(PathBuf::from) else {
            return Ok(());
        };
        if data_dir.exists() {
            return Err("test app-data path exists; refusing to reuse it".to_string());
        }
        let _cleanup = RemoveTestDirectory(data_dir.clone());
        fs::create_dir_all(&data_dir).map_err(|error| error.to_string())?;
        let sentinel = b"{\"version\":1,\"generated_at\":\"2026-10-16T00:00:00+00:00\",\"events\":[]}\n";
        fs::write(data_dir.join("ai-intelligence.json"), sentinel).map_err(|error| error.to_string())?;

        let (manifest, _payload_offset, manifest_hash) = read_payload_manifest(&executable)?;
        let runtime_root = data_dir.join("ai-runtime");
        fs::create_dir_all(&runtime_root).map_err(|error| error.to_string())?;
        let interrupted = runtime_root.join(format!(".staging-{manifest_hash}-interrupted"));
        fs::create_dir_all(&interrupted).map_err(|error| error.to_string())?;
        fs::write(interrupted.join("partial.bin"), b"partial").map_err(|error| error.to_string())?;

        let gate = Arc::new(Barrier::new(2));
        let mut installs = Vec::new();
        for _ in 0..2 {
            let gate = Arc::clone(&gate);
            let executable = executable.clone();
            let data_dir = data_dir.clone();
            installs.push(std::thread::spawn(move || {
                gate.wait();
                ensure_runtime(&executable, &data_dir)
            }));
        }
        let first = installs.remove(0).join().map_err(|_| "first install thread panicked".to_string())??;
        let second = installs.remove(0).join().map_err(|_| "second install thread panicked".to_string())??;
        assert_eq!(first, second, "concurrent first starts must share one cache");
        assert!(!interrupted.exists(), "interrupted staging must be recovered");
        assert!(verify_runtime(&first, &manifest, &manifest_hash));
        seed_feed_if_missing(&first, &data_dir)?;
        assert_eq!(fs::read(data_dir.join("ai-intelligence.json")).map_err(|error| error.to_string())?, sentinel);

        let alias = data_dir.join("移动改名 Cubic.exe");
        std::fs::hard_link(&executable, &alias).map_err(|error| format!("same-volume rename test link failed: {error}"))?;
        let reused = ensure_runtime(&alias, &data_dir)?;
        assert_eq!(reused, first, "renamed EXE must reuse the same per-user runtime");
        fs::remove_file(&alias).map_err(|error| error.to_string())?;

        let schema = first.join("schema/summary-schema.json");
        let original_schema = fs::read(&schema).map_err(|error| error.to_string())?;
        fs::write(&schema, b"corrupt").map_err(|error| error.to_string())?;
        let repaired = ensure_runtime(&executable, &data_dir)?;
        assert_eq!(repaired, first);
        assert_eq!(fs::read(&schema).map_err(|error| error.to_string())?, original_schema);
        assert_eq!(fs::read(data_dir.join("ai-intelligence.json")).map_err(|error| error.to_string())?, sentinel);

        let python = first.join("python/python.exe");
        let python_check = Command::new(&python)
            .args(["-c", "import json,sqlite3,ssl,urllib.request,ai_radar.watch; print('embedded runtime OK')"])
            .current_dir(&first)
            .output()
            .map_err(|error| format!("embedded Python failed to start: {error}"))?;
        if !python_check.status.success() {
            return Err(String::from_utf8_lossy(&python_check.stderr).into_owned());
        }

        let prompt = data_dir.join("offline-prompt.txt");
        fs::write(
            &prompt,
            "<|im_start|>system\nCopy one exact contiguous excerpt from the evidence and return one JSON result with the exact event_key.\n<|im_end|>\n<|im_start|>user\nItems: [{\"event_key\":\"offline-smoke\",\"evidence\":\"Synthetic offline evidence says the local endpoint was added.\"}]\n/no_think\n<|im_end|>\n<|im_start|>assistant\n",
        )
        .map_err(|error| error.to_string())?;
        let runner = first.join("llama").join("llama-completion.exe");
        let model_arg = Path::new("model").join("Qwen3-0.6B-Q4_0.gguf");
        let model_arg = model_arg.to_str().ok_or_else(|| "model path is not Unicode".to_string())?;
        let schema_arg = Path::new("schema").join("summary-schema.json");
        let schema_arg = schema_arg.to_str().ok_or_else(|| "schema path is not Unicode".to_string())?;
        let prompt_arg = Path::new("..").join("..").join("offline-prompt.txt");
        let prompt_arg = prompt_arg.to_str().ok_or_else(|| "prompt path is not Unicode".to_string())?;
        let mut command = Command::new(&runner);
        command.args([
            "-m", model_arg,
            "-c", "4096", "-n", "240", "-t", "2", "--temp", "0",
            "--no-display-prompt", "--simple-io", "--no-conversation",
            "--json-schema-file", schema_arg,
            "--file", prompt_arg,
        ]);
        command.current_dir(&first).stdout(std::process::Stdio::piped()).stderr(std::process::Stdio::piped());
        use std::os::windows::process::CommandExt;
        command.creation_flags(0x08000000);
        let job = KillOnCloseJob::new()?;
        let child = command.spawn().map_err(|error| format!("local CPU inference failed to start: {error}"))?;
        job.assign(&child)?;
        let output = child.wait_with_output().map_err(|error| error.to_string())?;
        if !output.status.success() {
            return Err(String::from_utf8_lossy(&output.stderr).into_owned());
        }
        let stdout = String::from_utf8_lossy(&output.stdout);
        if !stdout.contains("offline-smoke") || !stdout.contains("Synthetic offline evidence says the local endpoint was added.") {
            return Err("single-file offline inference did not return the synthetic evidence excerpt".to_string());
        }
        Ok(())
    }

    #[test]
    fn closing_the_worker_job_terminates_its_process_tree() -> Result<(), String> {
        use std::os::windows::process::CommandExt;
        let mut command = Command::new("cmd.exe");
        command.args(["/C", "ping -n 30 127.0.0.1 > NUL"]);
        command.creation_flags(0x08000000).stdout(std::process::Stdio::null()).stderr(std::process::Stdio::null());
        let mut child = command.spawn().map_err(|error| error.to_string())?;
        {
            let job = KillOnCloseJob::new()?;
            if let Err(error) = job.assign(&child) {
                let _ = child.kill();
                let _ = child.wait();
                return Err(error);
            }
        }
        let deadline = std::time::Instant::now() + Duration::from_secs(5);
        loop {
            if child.try_wait().map_err(|error| error.to_string())?.is_some() {
                return Ok(());
            }
            if std::time::Instant::now() >= deadline {
                let _ = child.kill();
                let _ = child.wait();
                return Err("worker process survived job-handle closure".to_string());
            }
            thread::sleep(Duration::from_millis(100));
        }
    }
}
