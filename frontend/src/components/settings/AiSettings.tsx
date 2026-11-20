"use client";

import { useCallback, useEffect, useState } from "react";

import { api, errorMessage, type AiSettings as Settings } from "@/lib/api";
import { useWorkspace } from "@/lib/workspace";

type Change = Partial<Pick<Settings, "auto_reply" | "writing_model" | "classify_model">>;

/** Auto-reply on/off and the AI models this workspace uses. Everyone sees them; admins change them. */
export function AiSettings() {
  const { can, toast } = useWorkspace();
  const [settings, setSettings] = useState<Settings | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const editable = can("admin");

  const load = useCallback(async () => {
    try {
      setSettings(await api<Settings>("/v1/workspace/ai-settings"));
    } catch (e) {
      setError(errorMessage(e));
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  async function save(change: Change, message: string) {
    setSaving(true);
    try {
      setSettings(await api<Settings>("/v1/workspace/ai-settings", { method: "PATCH", body: JSON.stringify(change) }));
      toast(message);
    } catch (e) {
      toast(errorMessage(e));
    } finally {
      setSaving(false);
    }
  }

  return (
    <section className="section">
      <div className="section-head">
        <h2 className="section-title">AI</h2>
        <p className="muted small">How Gazi answers buyers, and which models write for you.</p>
      </div>
      {error && <p className="notice notice-error">{error}</p>}
      {settings && (
        <div className="rows">
          <div className="row">
            <div className="row-main">
              <div>
                <div className="row-name" id="auto-reply-label">
                  Auto-reply
                </div>
                <div className="row-desc">
                  {settings.auto_reply
                    ? "Gazi sends replies by itself. When it isn't sure what a buyer meant, it saves a draft for you instead."
                    : "Every reply Gazi drafts waits for you to approve it."}
                </div>
              </div>
              <button
                type="button"
                role="switch"
                className="toggle"
                aria-checked={settings.auto_reply}
                aria-labelledby="auto-reply-label"
                disabled={!editable || saving}
                onClick={() =>
                  void save(
                    { auto_reply: !settings.auto_reply },
                    settings.auto_reply ? "Auto-reply is off" : "Auto-reply is on",
                  )
                }
              >
                <span className="toggle-knob" aria-hidden="true" />
              </button>
            </div>
          </div>
          <ModelRow
            id="writing-model"
            label="Writing model"
            desc="Writes your sequences and replies."
            value={settings.writing_model}
            options={settings.available_models}
            disabled={!editable || saving}
            onChange={(model) => void save({ writing_model: model }, `Writing with ${model}`)}
          />
          <ModelRow
            id="classify-model"
            label="Sorting model"
            desc="Reads replies to tell interested buyers from the rest, and scores leads."
            value={settings.classify_model}
            options={settings.available_models}
            disabled={!editable || saving}
            onChange={(model) => void save({ classify_model: model }, `Sorting with ${model}`)}
          />
        </div>
      )}
      {settings && !editable && <p className="muted small" style={{ marginTop: 12 }}>Only admins can change these.</p>}
    </section>
  );
}

function ModelRow(props: {
  id: string;
  label: string;
  desc: string;
  value: string;
  options: string[];
  disabled: boolean;
  onChange: (value: string) => void;
}) {
  return (
    <div className="row">
      <div className="row-main">
        <div>
          <label className="row-name" htmlFor={props.id}>
            {props.label}
          </label>
          <div className="row-desc">{props.desc}</div>
        </div>
        <select
          id={props.id}
          className="input select"
          value={props.value}
          disabled={props.disabled}
          onChange={(e) => props.onChange(e.target.value)}
        >
          {props.options.map((model) => (
            <option key={model} value={model}>
              {model}
            </option>
          ))}
        </select>
      </div>
    </div>
  );
}
