//! Compiled, local Core utilities. JSON metadata never chooses executable code.
//! Rust policy authorizes calls before this adapter; no worker, network or eval.
use anyhow::{bail, Context, Result};
use serde::Deserialize;
use serde_json::{json, Value};
use std::time::{SystemTime, UNIX_EPOCH};
use tokio_util::sync::CancellationToken;

const MAX_NUMBER: f64 = 1e100;

pub fn owns(name: &str) -> bool {
    matches!(name, "get_time" | "get_date" | "calculate")
}
// Provider schemas keep numeric shape simple; Rust enforces finite magnitude
// bounds on every operand and intermediate result before execution.
pub fn schemas() -> Vec<Value> {
    [
        ("get_time", "Current time in UTC or a fixed UTC offset; IANA timezones are unsupported", json!({"timezone":{"type":"string","maxLength":9}}), vec![]),
        ("get_date", "Current Gregorian date in UTC or a fixed UTC offset", json!({"timezone":{"type":"string","maxLength":9},"format":{"type":"string","enum":["full","short","iso"]}}), vec![]),
        ("calculate", "Bounded finite arithmetic on explicit operands; no expressions or code", json!({"operation":{"type":"string","enum":["add","subtract","multiply","divide","remainder","power","sqrt","abs","min","max"]},"operands":{"type":"array","items":{"type":"number"},"minItems":1,"maxItems":32}}), vec!["operation","operands"]),
    ].into_iter().map(|(name, description, properties, required)| json!({
        "type":"function","name":name,"description":description,
        "parameters":{"type":"object","properties":properties,"required":required,"additionalProperties":false}
    })).collect()
}
fn utc() -> String { "UTC".into() }
fn full() -> String { "full".into() }
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct TimeArgs { #[serde(default="utc")] timezone: String }
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct DateArgs {
    #[serde(default="utc")] timezone: String,
    #[serde(default="full")] format: String,
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Arithmetic { operation: String, operands: Vec<f64> }

fn offset(zone: &str) -> Result<i64> {
    if zone == "UTC" { return Ok(0); }
    let b = zone.as_bytes();
    if b.len() != 9 || &b[..3] != b"UTC" || !matches!(b[3], b'+' | b'-')
        || b[6] != b':' || ![b[4], b[5], b[7], b[8]].iter().all(u8::is_ascii_digit) {
        bail!("Unsupported timezone; use UTC or UTC+HH:MM/UTC-HH:MM");
    }
    let hours = i64::from((b[4]-b'0')*10 + b[5]-b'0');
    let minutes = i64::from((b[7]-b'0')*10 + b[8]-b'0');
    if hours > 14 || minutes > 59 || (hours == 14 && minutes != 0) {
        bail!("Timezone offset must be within -14:00..+14:00");
    }
    Ok((hours*3600 + minutes*60) * if b[3] == b'-' { -1 } else { 1 })
}
// Civil-date conversion from a Gregorian day ordinal; arithmetic is bounded
// to years 0001..9999 before conversion so unsupported clocks fail explicitly.
fn civil(days: i64) -> (i64, i64, i64) {
    let z = days + 719468;
    let era = z.div_euclid(146097);
    let doe = z - era*146097;
    let yoe = (doe - doe/1460 + doe/36524 - doe/146096)/365;
    let y = yoe + era*400;
    let doy = doe - (365*yoe + yoe/4 - yoe/100);
    let mp = (5*doy + 2)/153;
    let d = doy - (153*mp+2)/5 + 1;
    let m = mp + if mp < 10 { 3 } else { -9 };
    (y + if m <= 2 { 1 } else { 0 }, m, d)
}
fn clock(name: &str, args: &Value, timestamp: i64) -> Result<Value> {
    let (zone, format) = if name == "get_time" {
        let a: TimeArgs = serde_json::from_value(args.clone()).context("Invalid time arguments")?;
        (a.timezone, "time".to_owned())
    } else {
        let a: DateArgs = serde_json::from_value(args.clone()).context("Invalid date arguments")?;
        if !matches!(a.format.as_str(), "full" | "short" | "iso") { bail!("Invalid date format"); }
        (a.timezone, a.format)
    };
    let delta = offset(&zone)?;
    let local = timestamp.checked_add(delta).context("Clock out of range")?;
    if !(-62135596800..=253402300799).contains(&local) { bail!("Clock out of range"); }
    let days = local.div_euclid(86400);
    let seconds = local.rem_euclid(86400);
    let (year, month, day) = civil(days);
    let date = format!("{year:04}-{month:02}-{day:02}");
    let time = format!("{:02}:{:02}:{:02}", seconds/3600, seconds/60%60, seconds%60);
    let text = match format.as_str() {
        "time" => time,
        "short" => date,
        "iso" => format!("{date}T{time}{}", if zone == "UTC" { "+00:00" } else { &zone[3..] }),
        _ => {
            let weekdays = ["Sunday","Monday","Tuesday","Wednesday","Thursday","Friday","Saturday"];
            let months = ["January","February","March","April","May","June","July","August","September","October","November","December"];
            format!("{}, {} {}, {}", weekdays[(days+4).rem_euclid(7) as usize], months[(month-1) as usize], day, year)
        }
    };
    Ok(json!({"text":text,"timezone":zone,"unix_seconds":timestamp,"precision":"seconds"}))
}
fn finite(value: f64) -> Result<f64> {
    if !value.is_finite() || value.abs() > MAX_NUMBER { bail!("Arithmetic result exceeds finite magnitude limit 1e100"); }
    Ok(if value == 0.0 { 0.0 } else { value })
}
fn arithmetic(args: Arithmetic) -> Result<Value> {
    if !(1..=32).contains(&args.operands.len()) { bail!("Expected 1..32 operands"); }
    for &operand in &args.operands { finite(operand)?; }
    let unary = matches!(args.operation.as_str(), "sqrt" | "abs");
    let binary = matches!(args.operation.as_str(), "power" | "remainder");
    if unary && args.operands.len() != 1 || binary && args.operands.len() != 2 {
        bail!("Wrong operand count for operation");
    }
    let first = args.operands[0];
    let result = match args.operation.as_str() {
        "sqrt" => { if first < 0.0 { bail!("Square root requires a nonnegative operand"); } finite(first.sqrt())? }
        "abs" => finite(first.abs())?,
        "power" => {
            if args.operands[1].abs() > 1024.0 { bail!("Exponent magnitude exceeds 1024"); }
            finite(first.powf(args.operands[1]))?
        }
        "remainder" => { if args.operands[1] == 0.0 { bail!("Division by zero"); } finite(first % args.operands[1])? }
        "add" | "subtract" | "multiply" | "divide" | "min" | "max" => {
            let mut result = first;
            for &next in &args.operands[1..] {
                result = finite(match args.operation.as_str() {
                    "add" => result + next, "subtract" => result - next,
                    "multiply" => result * next,
                    "divide" => { if next == 0.0 { bail!("Division by zero"); } result / next }
                    "min" => result.min(next), "max" => result.max(next), _ => unreachable!()
                })?;
            }
            result
        }
        _ => bail!("Unsupported arithmetic operation"),
    };
    Ok(json!({"result":result,"operation":args.operation,"operand_count":args.operands.len(),"precision":"f64"}))
}
pub async fn execute(name: &str, args: &Value, cancel: CancellationToken) -> Result<Value> {
    if cancel.is_cancelled() { bail!("tool execution cancelled"); }
    let output = match name {
        "get_time" | "get_date" => match SystemTime::now().duration_since(UNIX_EPOCH) {
            Ok(now) => i64::try_from(now.as_secs()).context("Clock out of range")
                .and_then(|timestamp| clock(name, args, timestamp)),
            Err(_) => Err(anyhow::anyhow!("System clock before Unix epoch")),
        },
        "calculate" => serde_json::from_value(args.clone()).context("Invalid arithmetic arguments").and_then(arithmetic),
        _ => bail!("Unknown Core utility tool"),
    };
    if cancel.is_cancelled() { bail!("tool execution cancelled"); }
    Ok(match output {
        Ok(value) => json!({"status":"success","output":value}),
        Err(error) => json!({"status":"error","output":error.to_string(),"error_kind":"invalid_input_or_clock"}),
    })
}
#[cfg(test)]
mod tests {
    use super::*;
    fn calc(op: &str, numbers: Value) -> Result<Value> {
        arithmetic(serde_json::from_value(json!({"operation":op,"operands":numbers}))?)
    }
    #[test]
    fn arithmetic_is_finite_and_never_interprets_code() {
        assert_eq!(calc("subtract",json!([12,2,3])).unwrap()["result"],7.0);
        assert_eq!(calc("power",json!([2,10])).unwrap()["result"],1024.0);
        assert_eq!(calc("sqrt",json!([144])).unwrap()["result"],12.0);
        for (op, numbers) in [("divide",json!([1,0])),("multiply",json!([1e100,10])),
            ("power",json!([10,1025])),("power",json!([-2,0.5])),
            ("sqrt",json!([-1])),("abs",json!([1,2])),("remainder",json!([1])),
            ("add",json!([])),("eval",json!([1]))] {
            assert!(calc(op,numbers).is_err());
        }
        assert!(calc("add",json!(vec![1;33])).is_err());
        assert!(serde_json::from_value::<Arithmetic>(json!({"operation":"add","operands":[1],"expression":"1+1"})).is_err());
        assert!(calc("add",json!([1e101])).is_err());
    }
    #[test]
    fn clock_handles_offsets_leap_days_and_limits() {
        assert_eq!(clock("get_date",&json!({"format":"full"}),0).unwrap()["text"],"Thursday, January 1, 1970");
        assert_eq!(clock("get_date",&json!({"format":"short","timezone":"UTC-01:00"}),0).unwrap()["text"],"1969-12-31");
        assert_eq!(clock("get_date",&json!({"format":"iso","timezone":"UTC+14:00"}),951782400).unwrap()["text"],"2000-02-29T14:00:00+14:00");
        assert_eq!(clock("get_time",&json!({"timezone":"UTC+05:30"}),0).unwrap()["text"],"05:30:00");
        assert_eq!(clock("get_date",&json!({"format":"short"}),253402300799).unwrap()["text"],"9999-12-31");
        for zone in ["America/Detroit","UTC+14:01","UTC+00:60","UTC+١٠:00","UTC../../"] {
            assert!(offset(zone).is_err());
        }
        assert!(clock("get_date",&json!({"format":"short"}),i64::MAX).is_err());
        assert!(clock("get_time",&json!({"format":"iso"}),0).is_err());
    }
    #[tokio::test]
    async fn native_execution_reports_errors_and_obeys_cancellation() {
        let call=execute("calculate",&json!({"operation":"add","operands":[2,3]}),CancellationToken::new()).await.unwrap();
        assert_eq!(call["status"],"success"); assert_eq!(call["output"]["result"],5.0);
        assert_eq!(execute("calculate",&json!({"expression":"2+2"}),CancellationToken::new()).await.unwrap()["status"],"error");
        assert_eq!(execute("get_time",&json!({}),CancellationToken::new()).await.unwrap()["status"],"success");
        let cancel=CancellationToken::new();cancel.cancel();
        assert!(execute("calculate",&json!({"operation":"add","operands":[1]}),cancel).await.is_err());
    }
}
