#![cfg(target_os = "linux")]

use std::{
    env,
    path::PathBuf,
    process::ExitCode,
    sync::{Arc, atomic::AtomicBool},
};

use fleet_control::{ControlConfig, run};

fn main() -> ExitCode {
    match execute() {
        Ok(()) => ExitCode::SUCCESS,
        Err(error) => {
            eprintln!("fleet managed control failed: {error}");
            ExitCode::FAILURE
        }
    }
}

fn execute() -> fleet_control::Result<()> {
    let arguments = parse_arguments().map_err(|_| fleet_control::ControlError::MalformedRequest)?;
    let config = ControlConfig::new(
        arguments.socket,
        arguments.database,
        arguments.allowed_uid,
        arguments.socket_gid,
    )?;
    let shutdown = Arc::new(AtomicBool::new(false));
    signal_hook::flag::register(libc::SIGTERM, Arc::clone(&shutdown))?;
    signal_hook::flag::register(libc::SIGINT, Arc::clone(&shutdown))?;
    run(config, shutdown)
}

struct Arguments {
    socket: PathBuf,
    database: PathBuf,
    allowed_uid: u32,
    socket_gid: Option<u32>,
}

fn parse_arguments() -> std::result::Result<Arguments, ()> {
    let mut socket = None;
    let mut database = None;
    let mut allowed_uid = None;
    let mut socket_gid = None;
    let mut values = env::args_os().skip(1);
    while let Some(flag) = values.next() {
        let value = values.next().ok_or(())?;
        match flag.to_str().ok_or(())? {
            "--socket" if socket.is_none() => socket = Some(PathBuf::from(value)),
            "--database" if database.is_none() => database = Some(PathBuf::from(value)),
            "--allowed-uid" if allowed_uid.is_none() => {
                allowed_uid = Some(value.to_str().ok_or(())?.parse().map_err(|_| ())?)
            }
            "--socket-gid" if socket_gid.is_none() => {
                socket_gid = Some(value.to_str().ok_or(())?.parse().map_err(|_| ())?)
            }
            _ => return Err(()),
        }
    }
    Ok(Arguments {
        socket: socket.ok_or(())?,
        database: database.ok_or(())?,
        allowed_uid: allowed_uid.ok_or(())?,
        socket_gid,
    })
}
