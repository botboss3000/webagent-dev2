//! Provider-reported accounting is separate from the byte-measured model envelope.
use crate::*;
pub(crate) fn reported(response:&Value)->Option<Value>{
    let usage=&response["usage"];
    let input=usage["input_tokens"].as_u64().or_else(||usage["prompt_tokens"].as_u64())?;
    let output=usage["output_tokens"].as_u64().or_else(||usage["completion_tokens"].as_u64())?;
    if input>1_000_000_000 || output>1_000_000_000{return None;}
    let cached=usage["input_tokens_details"]["cached_tokens"].as_u64()
        .or_else(||usage["prompt_tokens_details"]["cached_tokens"].as_u64())
        .or_else(||usage["prompt_cache_hit_tokens"].as_u64()).or_else(||usage["cached_input_tokens"].as_u64()).filter(|v|*v<=input);
    let cost=usage["cost"].as_f64().or_else(||usage["cost_usd"].as_f64()).filter(|v|v.is_finite() && *v>=0. && *v<=1_000_000.);
    Some(json!({"input_tokens":input,"output_tokens":output,"cached_input_tokens":cached,"cost_usd":cost,"source":"provider"}))
}
pub(crate) fn record(run:&mut Value,response:&Value){
    let sample=reported(response);
    let mut usage=if run["usage"].is_object(){run["usage"].clone()}else{json!({"input_tokens":0,"output_tokens":0,"cached_input_tokens":0,"reported_calls":0,"unreported_calls":0,"known_cost_usd":0.0,"unknown_cost_calls":0,"unknown_cache_calls":0})};
    if let Some(sample)=&sample{
        for field in ["input_tokens","output_tokens","cached_input_tokens"]{
            usage[field]=json!(usage[field].as_u64().unwrap_or(0).saturating_add(sample[field].as_u64().unwrap_or(0)));
        }
        if sample["cached_input_tokens"].is_null(){usage["unknown_cache_calls"]=json!(usage["unknown_cache_calls"].as_u64().unwrap_or(0)+1);}
        usage["reported_calls"]=json!(usage["reported_calls"].as_u64().unwrap_or(0)+1);
        if let Some(cost)=sample["cost_usd"].as_f64(){
            usage["known_cost_usd"]=json!(usage["known_cost_usd"].as_f64().unwrap_or(0.)+cost);
        }else{usage["unknown_cost_calls"]=json!(usage["unknown_cost_calls"].as_u64().unwrap_or(0)+1);}
    }else{
        usage["unknown_cache_calls"]=json!(usage["unknown_cache_calls"].as_u64().unwrap_or(0)+1);
        usage["unreported_calls"]=json!(usage["unreported_calls"].as_u64().unwrap_or(0)+1);
        usage["unknown_cost_calls"]=json!(usage["unknown_cost_calls"].as_u64().unwrap_or(0)+1);
    }
    usage["latest"]=sample.unwrap_or(Value::Null);
    run["usage"]=usage;
}
// A fixed numeric projection cannot disclose provider extras or request context.
pub(crate) fn public(value:&Value)->Value{
    if !value.is_object(){return Value::Null;}
    let mut result=serde_json::Map::new();
    for field in ["input_tokens","output_tokens","cached_input_tokens","reported_calls","unreported_calls","unknown_cost_calls","unknown_cache_calls"]{
        result.insert(field.into(),json!(value[field].as_u64().unwrap_or(0)));
    }
    result.insert("known_cost_usd".into(),json!(value["known_cost_usd"].as_f64().filter(|v|v.is_finite()&&*v>=0.)));
    if let Some(sample)=reported(&json!({"usage":value["latest"]})){result.insert("latest".into(),sample);}
    Value::Object(result)
}
#[cfg(test)]
mod tests{
    use super::*;
    #[test]fn usage_is_reported_bounded_and_private(){
        let mut run=json!({});
        record(&mut run,&json!({"usage":{"prompt_tokens":20,"completion_tokens":5,"prompt_cache_hit_tokens":4,"cost":0.12,"private":"secret"}}));
        record(&mut run,&json!({"usage":{"input_tokens":10,"output_tokens":2}}));
        record(&mut run,&json!({}));
        let out=public(&run["usage"]);
        assert_eq!(out["input_tokens"],30);assert_eq!(out["output_tokens"],7);
        assert_eq!(out["cached_input_tokens"],4);assert_eq!(out["reported_calls"],2);
        assert_eq!(out["unreported_calls"],1);assert_eq!(out["unknown_cost_calls"],2);
        assert!(!out.to_string().contains("secret"));
        assert!(reported(&json!({"usage":{"input_tokens":-1,"output_tokens":4}})).is_none());
    }
}
