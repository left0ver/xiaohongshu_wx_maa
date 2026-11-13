use crate::{AppError, AppState};
use serde_json::{json, Value};
#[cfg(target_os = "android")]
use tauri::Manager;

#[tauri::command]
pub async fn wechat_account(config: Value) -> Result<Value, AppError> {
    #[cfg(target_os = "android")]
    {
        tokio::task::spawn_blocking(move || {
            use jni::objects::{JObject, JString, JValue};
            let vm = crate::runtime::java_vm().ok_or_else(|| AppError::Message("Android runtime unavailable".into()))?;
            let mut env = vm.attach_current_thread().map_err(|error| AppError::Message(error.to_string()))?;
            let class = crate::runtime::runtime_bridge_class()?;
            let value = env.new_string(config.to_string()).map_err(|error| AppError::Message(error.to_string()))?;
            let object = JObject::from(value);
            let called = env.call_static_method(class, "wechatAccount", "(Ljava/lang/String;)Ljava/lang/String;", &[JValue::Object(&object)]);
            let returned = match called {
                Ok(value) => value,
                Err(error) => {
                    let exception = env.exception_occurred().map_err(|failure| AppError::Message(failure.to_string()))?;
                    env.exception_clear().map_err(|failure| AppError::Message(failure.to_string()))?;
                    if exception.is_null() { return Err(AppError::Message(error.to_string())); }
                    let description = env.call_method(exception, "toString", "()Ljava/lang/String;", &[])
                        .map_err(|failure| AppError::Message(failure.to_string()))?.l()
                        .map_err(|failure| AppError::Message(failure.to_string()))?;
                    let message: String = env.get_string(&JString::from(description))
                        .map_err(|failure| AppError::Message(failure.to_string()))?.into();
                    return Err(AppError::Message(message));
                }
            };
            let result = returned.l().map_err(|error| AppError::Message(error.to_string()))?;
            let value: String = env.get_string(&JString::from(result)).map_err(|error| AppError::Message(error.to_string()))?.into();
            serde_json::from_str(&value).map_err(|error| AppError::Message(error.to_string()))
        }).await.map_err(|error| AppError::Message(error.to_string()))?
    }
    #[cfg(not(target_os = "android"))]
    { let _ = config; Err(AppError::Message("Android is required".into())) }
}

#[tauri::command]
pub fn wechat_status() -> Result<Value, AppError> {
    #[cfg(target_os = "android")]
    {
        let text = crate::call_runtime_bridge_optional_string("wechatStatus")?.ok_or_else(|| AppError::Message("Account unavailable".into()))?;
        serde_json::from_str(&text).map_err(|error| AppError::Message(error.to_string()))
    }
    #[cfg(not(target_os = "android"))]
    { Ok(json!({"connected":false,"status":"Android is required","message":"","machine":""})) }
}

pub fn task(project: &crate::domain::types::Project, config: &Value) -> Result<crate::domain::types::ResolvedTask, AppError> {
    let name = config["task"].as_str().ok_or_else(|| AppError::Message("Missing task".into()))?;
    let source = if name == "sync_discovery" { "collect_channels" } else { name };
    let mut task = project.tasks.iter().find(|task| task.name == source).cloned()
        .ok_or_else(|| AppError::Message("Unsupported task".into()))?;
    if project.name != "wechat_channels" || !config["options"].is_object() || !config["remote"].is_object() {
        return Err(AppError::Message("Invalid remote task".into()));
    }
    task.entry = "RemoteWechatTask".into();
    Ok(crate::domain::types::ResolvedTask {
        task, configured: None, enabled: true, unavailable_reason: None,
        pipeline_override: json!({"RemoteWechatTask":{"recognition":"DirectHit","action":"Custom",
            "custom_action":"RemoteWechatTask","custom_action_param":config,"next":[]}}),
    })
}

#[cfg(target_os = "android")]
#[no_mangle]
pub extern "system" fn Java_top_natsuu_mta_WechatRemoteService_nativeStart(
    mut env: jni::JNIEnv, _class: jni::objects::JClass, config: jni::objects::JString,
) -> jni::sys::jstring {
    let outcome = (|| -> Result<Value, AppError> {
        let source: String = env.get_string(&config).map_err(|error| AppError::Message(error.to_string()))?.into();
        let config: Value = serde_json::from_str(&source).map_err(|error| AppError::Message(error.to_string()))?;
        let app = crate::android_app_handle().ok_or_else(|| AppError::Message("App is not ready".into()))?;
        let state = app.state::<AppState>();
        if state.maa.status() != crate::runtime::RunState::Idle { return Err(AppError::Message("手机已有任务正在执行".into())); }
        let result = tauri::async_runtime::block_on(crate::start_run_core(app.clone(), state.inner(), None, None, None, Some(config)))?;
        Ok(json!({"success":true,"executionId":result.execution_id}))
    })();
    let result = outcome.unwrap_or_else(|error| json!({"success":false,"message":error.to_string()}));
    env.new_string(result.to_string()).expect("JNI result").into_raw()
}

#[cfg(target_os = "android")]
#[no_mangle]
pub extern "system" fn Java_top_natsuu_mta_WechatRemoteService_nativeStatus(
    env: jni::JNIEnv, _class: jni::objects::JClass,
) -> jni::sys::jstring {
    let result = crate::runtime::run_result().map(|result| serde_json::to_value(result).expect("RunResult JSON"))
        .unwrap_or_else(|| json!({"state":"Idle"}));
    env.new_string(result.to_string()).expect("JNI result").into_raw()
}

#[cfg(target_os = "android")]
#[no_mangle]
pub extern "system" fn Java_top_natsuu_mta_WechatRemoteService_nativeStop(
    _env: jni::JNIEnv, _class: jni::objects::JClass,
) {
    if let Some(app) = crate::android_app_handle() {
        let state = app.state::<AppState>();
        if let Err(error) = crate::request_run_stop(app, state.inner(), None) { log::error!("Could not stop remote task: {error}"); }
    }
}
