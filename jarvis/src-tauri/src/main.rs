mod window_effects;

use serde::Deserialize;
use std::{
    fs,
    net::TcpStream,
    path::{Path, PathBuf},
    process::{Child, Command},
    sync::Mutex,
    thread,
    time::Duration,
};
use tauri::{
    menu::{Menu, MenuItem},
    tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent},
    Emitter, Manager, WebviewUrl, WebviewWindowBuilder,
};
use tauri_plugin_global_shortcut::GlobalShortcutExt;

struct BackendProcess(Mutex<Option<Child>>);

/// S2: WS-токен, полученный от backend один раз за запуск лаунчера.
///
/// Хранится только в памяти процесса и отдаётся webview по `invoke`, а не
/// параметром URL: адрес соединения попадает в логи и историю, токен — нет.
struct WsAuthToken(Mutex<Option<String>>);

/// Спрашивает токен у backend: `<program> <args…> --print-ws-token`.
///
/// Backend сам генерирует токен при первом запуске и хранит его зашифрованным
/// (`core/security/ws_token.py`), поэтому лаунчеру не нужны ни ключи, ни
/// доступ к DPAPI. Токен — последняя непустая строка stdout.
fn read_ws_token(settings: &LauncherSettings) -> Option<String> {
    if let Ok(existing) = std::env::var("JARVIS_WS_TOKEN") {
        let existing = existing.trim().to_string();
        if !existing.is_empty() {
            return Some(existing);
        }
    }
    let command = settings.backend_command.clone().unwrap_or_default();
    let (program, args) = command.split_first()?;
    let root = project_root();
    let program = resolve_backend_program(program, &root);
    let mut builder = Command::new(program);
    builder
        .args(args)
        .arg("--print-ws-token")
        .current_dir(&root)
        .env("JARVIS_HOME", &root)
        .env("PYTHONUTF8", "1")
        .env("PYTHONIOENCODING", "utf-8");
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        builder.creation_flags(0x08000000);
    }
    let output = builder.output().ok()?;
    if !output.status.success() {
        return None;
    }
    String::from_utf8_lossy(&output.stdout)
        .lines()
        .map(str::trim)
        .filter(|line| !line.is_empty())
        .last()
        .map(str::to_string)
}

#[tauri::command]
fn ws_auth_token(state: tauri::State<WsAuthToken>) -> Result<String, String> {
    let mut guard = state
        .0
        .lock()
        .map_err(|_| "ws token state poisoned".to_string())?;
    if let Some(token) = guard.as_ref() {
        return Ok(token.clone());
    }
    let token = read_ws_token(&launcher_settings())
        .ok_or_else(|| "backend не выдал WS-токен".to_string())?;
    *guard = Some(token.clone());
    Ok(token)
}

fn stop_backend(app: &tauri::AppHandle) {
    let state = app.state::<BackendProcess>();
    let Ok(mut guard) = state.0.lock() else { return };
    let Some(mut child) = guard.take() else { return };
    #[cfg(windows)]
    {
        use std::process::Stdio;
        let _ = Command::new("taskkill")
            .args(["/PID", &child.id().to_string(), "/T", "/F"])
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .status();
    }
    let _ = child.kill();
    let _ = child.wait();
}

impl Drop for BackendProcess {
    fn drop(&mut self) {
        if let Ok(mut child) = self.0.lock() {
            if let Some(process) = child.as_mut() {
                let _ = process.kill();
                let _ = process.wait();
            }
            *child = None;
        }
    }
}
#[derive(Clone, Deserialize)]
struct SettingsFile {
    launcher: Option<LauncherSettings>,
}
#[derive(Clone, Deserialize)]
struct LauncherSettings {
    autostart: Option<bool>,
    hotkey: Option<String>,
    backend_command: Option<Vec<String>>,
    backend_workdir: Option<String>,
}
impl Default for LauncherSettings {
    fn default() -> Self {
        Self {
            autostart: Some(false),
            hotkey: Some("Ctrl+Space".into()),
            backend_command: Some(vec!["python".into(), "-m".into(), "core.ws_server".into()]),
            backend_workdir: Some(String::new()),
        }
    }
}

fn is_project_root(path: &Path) -> bool {
    // A packaged app keeps the Python tree under resources/jarvis-runtime.
    // Do not mistake the install directory (which only contains `resources`)
    // for the runtime root, otherwise launcher settings fall back to `python`
    // and the bundled backend never starts.
    path.join("config").is_dir() && (path.join("core").is_dir() || path.join("runtime").is_dir())
}

fn project_root() -> PathBuf {
    let compiled = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../..");
    let mut candidates: Vec<PathBuf> = Vec::new();
    if let Ok(home) = std::env::var("JARVIS_HOME") {
        candidates.push(PathBuf::from(home));
    }
    if let Ok(exe) = std::env::current_exe() {
        if let Some(parent) = exe.parent() {
            candidates.push(parent.join("resources/jarvis-runtime"));
            candidates.push(parent.join("resources"));
            candidates.push(parent.to_path_buf());
            candidates.push(parent.join("resources/project"));
        }
        candidates.extend(exe.ancestors().map(PathBuf::from));
    }
    candidates.push(std::env::current_dir().unwrap_or_default());
    candidates.push(compiled.clone());
    candidates
        .into_iter()
        .find(|path| is_project_root(path))
        .and_then(|path| path.canonicalize().ok())
        .unwrap_or(compiled)
}
fn launcher_settings() -> LauncherSettings {
    fs::read_to_string(project_root().join("config/settings.json"))
        .ok()
        .and_then(|raw| serde_json::from_str::<SettingsFile>(&raw).ok())
        .and_then(|config| config.launcher)
        .unwrap_or_default()
}
fn backend_is_listening() -> bool {
    TcpStream::connect_timeout(
        &"127.0.0.1:8771".parse().expect("socket"),
        Duration::from_millis(150),
    )
    .is_ok()
}
fn resolve_backend_program(program: &str, root: &Path) -> String {
    // `Command::current_dir` changes the child working directory after the
    // executable has been resolved.  A packaged command such as
    // `runtime/jarvis-backend.exe` therefore must be made absolute first;
    // otherwise Windows resolves it relative to the frontend's own cwd and
    // the UI stays in CONNECTING forever.
    let configured = PathBuf::from(program);
    if configured.is_relative() {
        let bundled = root.join(&configured);
        if bundled.is_file() {
            return bundled.to_string_lossy().into_owned();
        }
    }
    if !program.eq_ignore_ascii_case("python") && !program.eq_ignore_ascii_case("pythonw") {
        return program.to_string();
    }
    let mut candidates = Vec::new();
    if let Ok(value) = std::env::var("JARVIS_PYTHON") {
        candidates.push(PathBuf::from(value));
    }
    let root = project_root();
    candidates.push(root.join("runtime/pythonw.exe"));
    candidates.push(root.join("runtime/python.exe"));
    candidates.push(root.join(".venv/Scripts/pythonw.exe"));
    candidates.push(root.join(".venv/Scripts/python.exe"));
    if let Ok(home) = std::env::var("USERPROFILE") {
        let root = PathBuf::from(home);
        candidates.push(root.join("AppData/Local/hermes/hermes-agent/venv/Scripts/pythonw.exe"));
        candidates.push(root.join("AppData/Local/hermes/hermes-agent/venv/Scripts/python.exe"));
        candidates.push(root.join("venv/Scripts/pythonw.exe"));
        candidates.push(root.join("venv/Scripts/python.exe"));
    }
    candidates.push(PathBuf::from("pythonw.exe"));
    candidates.push(PathBuf::from("python.exe"));
    candidates
        .into_iter()
        .find(|candidate| candidate.is_file())
        .map(|candidate| candidate.to_string_lossy().into_owned())
        .unwrap_or_else(|| program.to_string())
}
fn spawn_backend(settings: &LauncherSettings) -> Option<Child> {
    if backend_is_listening() {
        return None;
    }
    let command = settings.backend_command.clone().unwrap_or_default();
    let (program, args) = command.split_first()?;
    let root = project_root();
    let program = resolve_backend_program(program, &root);
    let dir = settings
        .backend_workdir
        .as_deref()
        .filter(|value| !value.is_empty())
        .map(PathBuf::from)
        .filter(|value| value.is_dir())
        .unwrap_or_else(|| root.clone());
    let runtime_temp = std::env::var_os("JARVIS_RUNTIME_TEMP")
        .map(PathBuf::from)
        .unwrap_or_else(|| root.join("runtime-temp"));
    let _ = fs::create_dir_all(&runtime_temp);
    let browser_runtime = [
        root.join("runtime/playwright-browsers"),
        root.join("jarvis/src-tauri/target/release/resources/jarvis-runtime/runtime/playwright-browsers"),
    ]
    .into_iter()
    .find(|candidate| candidate.join("chromium-1234").is_dir());
    let mut builder = Command::new(program);
    builder
        .args(args)
        .current_dir(&dir)
        .env("JARVIS_HOME", &root)
        .env("PYTHONUTF8", "1")
        .env("PYTHONIOENCODING", "utf-8")
        .env("PYTHONUNBUFFERED", "1")
        // PyInstaller onefile extracts the bundled backend before Python
        // starts. Keep that transient 214 MB payload beside the project so
        // a full system TEMP volume cannot strand the GUI in STARTING.
        .env("JARVIS_RUNTIME_TEMP", &runtime_temp)
        .env("TEMP", &runtime_temp)
        .env("TMP", &runtime_temp);
    if let Some(browser_runtime) = browser_runtime {
        builder.env("PLAYWRIGHT_BROWSERS_PATH", browser_runtime);
    }
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        builder.creation_flags(0x08000000);
    }
    match builder.spawn() {
        Ok(child) => Some(child),
        Err(error) => {
            let _ = fs::create_dir_all(root.join("logs"));
            let _ = fs::write(
                root.join("logs/backend-launch-error.txt"),
                format!("backend launch failed in {}: {}\n", dir.display(), error),
            );
            None
        }
    }
}
fn manage_backend(app: tauri::AppHandle, settings: LauncherSettings) {
    thread::spawn(move || loop {
        let state = app.state::<BackendProcess>();
        let mut child = match state.0.lock() {
            Ok(guard) => guard,
            Err(_) => break,
        };
        let restart = child
            .as_mut()
            .map(|process| process.try_wait().ok().flatten().is_some())
            .unwrap_or(!backend_is_listening());
        if restart {
            *child = spawn_backend(&settings);
        }
        drop(child);
        thread::sleep(Duration::from_secs(5));
    });
}

#[cfg(windows)]
fn sync_autostart(enabled: bool) {
    use winreg::{enums::HKEY_CURRENT_USER, RegKey};
    let hkcu = RegKey::predef(HKEY_CURRENT_USER);
    if let Ok((run, _)) = hkcu.create_subkey("Software\\Microsoft\\Windows\\CurrentVersion\\Run") {
        if enabled {
            if let Ok(exe) = std::env::current_exe() {
                let _ = run.set_value("JARVIS", &format!("\"{}\" --hidden", exe.display()));
            }
        } else {
            let _ = run.delete_value("JARVIS");
        }
    }
}
#[cfg(not(windows))]
fn sync_autostart(_: bool) {}
fn show_main(app: &tauri::AppHandle) {
    if let Some(window) = app.get_webview_window("main") {
        let _ = window.show();
        let _ = window.set_focus();
    }
}
fn show_overlay(app: &tauri::AppHandle) {
    if let Some(window) = app.get_webview_window("overlay") {
        let _ = window.show();
        let _ = window.set_focus();
    }
    let _ = app.emit("jarvis://hotkey", ());
}

fn main() {
    let settings = launcher_settings();
    let hidden = std::env::args().any(|arg| arg == "--hidden");
    tauri::Builder::default()
        .manage(BackendProcess(Mutex::new(None)))
        .manage(WsAuthToken(Mutex::new(None)))
        .invoke_handler(tauri::generate_handler![ws_auth_token])
        .plugin(tauri_plugin_shell::init())
        .plugin(tauri_plugin_opener::init())
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_fs::init())
        .plugin(tauri_plugin_http::init())
        .plugin(tauri_plugin_notification::init())
        .plugin(tauri_plugin_process::init())
        .plugin(tauri_plugin_global_shortcut::Builder::new().build())
        .setup(move |app| {
            let main = app
                .get_webview_window("main")
                .expect("main window configured");
            window_effects::setup_window_effects(&main);
            // `autostart` controls registration in Windows startup, not the
            // normal launch requested by the user. Only the explicit
            // `--hidden` startup entry stays tray-only.
            if hidden {
                let _ = main.hide();
            } else {
                let _ = main.show();
                let _ = main.set_focus();
            }
            WebviewWindowBuilder::new(app, "overlay", WebviewUrl::App("index.html".into()))
                .title("JARVIS")
                .inner_size(480.0, 48.0)
                .center()
                .decorations(false)
                .transparent(true)
                .always_on_top(true)
                .skip_taskbar(true)
                .visible(false)
                .build()?;
            let open = MenuItem::with_id(app, "open", "Открыть", true, None::<&str>)?;
            let settings_item =
                MenuItem::with_id(app, "settings", "Настройки", true, None::<&str>)?;
            let debug = MenuItem::with_id(app, "debug", "Debug", true, None::<&str>)?;
            let quit = MenuItem::with_id(app, "quit", "Выход", true, None::<&str>)?;
            let menu = Menu::with_items(app, &[&open, &settings_item, &debug, &quit])?;
            TrayIconBuilder::new()
                .icon(app.default_window_icon().expect("tray icon").clone())
                .menu(&menu)
                .on_menu_event(|app, event| match event.id().as_ref() {
                    "open" | "settings" | "debug" => show_main(app),
                    "quit" => {
                        stop_backend(app);
                        app.exit(0);
                    }
                    _ => {}
                })
                .on_tray_icon_event(|tray, event| {
                    if let TrayIconEvent::Click {
                        button: MouseButton::Left,
                        button_state: MouseButtonState::Up,
                        ..
                    } = event
                    {
                        show_main(tray.app_handle());
                    }
                })
                .build(app)?;
            let shortcut = settings
                .hotkey
                .clone()
                .unwrap_or_else(|| "Ctrl+Space".into());
            let hotkey_app = app.handle().clone();
            app.global_shortcut()
                .on_shortcut(shortcut.as_str(), move |_, _, _| show_overlay(&hotkey_app))?;
            sync_autostart(settings.autostart.unwrap_or(false));
            manage_backend(app.handle().clone(), settings.clone());
            // A missing profile is the one deliberate exception to tray-only
            // startup: after two quiet seconds, begin the first-meeting ritual.
            if !project_root().join("data/profile/profile.json").is_file() {
                let first_launch = app.handle().clone();
                thread::spawn(move || {
                    thread::sleep(Duration::from_secs(2));
                    show_main(&first_launch);
                });
            }
            Ok(())
        })
        .on_window_event(|window, event| {
            if window.label() == "main" {
                if let tauri::WindowEvent::CloseRequested { .. } = event {
                    // Closing the native GUI is the application shutdown
                    // boundary. Let Tauri exit so BackendProcess::drop kills
                    // the bundled backend instead of leaving a hidden server.
                    stop_backend(&window.app_handle());
                    window.app_handle().exit(0);
                }
            }
        })
        .run(tauri::generate_context!())
        .expect("error while running JARVIS");
}
