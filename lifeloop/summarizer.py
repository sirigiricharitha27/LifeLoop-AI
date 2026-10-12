"""Daily summary: Amazon Bedrock when configured, otherwise a labelled local fallback."""
import json, os

SYSTEM = ("You summarise a family caregiver's daily care records. Use ONLY the JSON records given. "
          "Cite evidence ids in square brackets like [event:3]. Never give medical advice or dosing. "
          "A missing record is NOT evidence that something is wrong; say what is unknown. "
          "Household events are simulated demo data. Write 3-5 plain sentences.")

def bedrock_summary(ctx):
    model = os.environ.get("BEDROCK_MODEL_ID")
    if not model: raise RuntimeError("BEDROCK_MODEL_ID is not set")
    import boto3  # optional dependency
    client = boto3.client("bedrock-runtime", region_name=os.environ.get("AWS_REGION", "us-east-1"))
    r = client.converse(modelId=model, system=[{"text": SYSTEM}],
                        messages=[{"role": "user", "content": [{"text": json.dumps(ctx)}]}],
                        inferenceConfig={"maxTokens": 500, "temperature": 0.2})
    return r["output"]["message"]["content"][0]["text"].strip(), model

def local_summary(ctx):
    n, items = ctx["elder_name"], ctx["timeline"]
    ev = [i for i in items if i["type"] == "event"]; rest = [i for i in items if i["type"] != "event"]
    lines = [f"Today for {n}: {len(items)} recent records are available."]
    for i in rest: lines.append(f"\u2022 {i['title']}: {i['label']} [source: {i['source_key']}; status: {i['status']}] [{i['id']}]")
    if ev: lines.append(f"\u2022 Household activity: {len(ev)} simulated events between {ev[0]['time']} and {ev[-1]['time']} [source: simulated_sensor; status: simulated] [{ev[0]['id']}..{ev[-1]['id']}]")
    if not any(i["type"] == "checkin" for i in items): lines.append(f"\u2022 No check-in response has been recorded from {n} yet.")
    if ctx.get("upcoming"): lines.append("Upcoming reminders: " + "; ".join(ctx["upcoming"]) + ".")
    for f in ctx["findings"]: lines.append(f["message"] + " This does not by itself mean anything is wrong.")
    lines.append("Important uncertainty: a missing record is not proof that an activity did not happen. Medication acknowledgements and reports do not verify ingestion. Simulated sensor events are labeled.")
    return "\n".join(lines)

def summarize(ctx):
    try:
        text, model = bedrock_summary(ctx)
        return {"text": text, "source": "bedrock", "model": model}
    except Exception as e:  # any failure -> honest fallback
        return {"text": local_summary(ctx), "source": "local_fallback", "fallback_reason": f"{type(e).__name__}: {e}"[:200]}
