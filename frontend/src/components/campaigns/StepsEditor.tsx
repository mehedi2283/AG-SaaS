"use client";

import { useEffect, useState } from "react";

import { api, errorMessage, type Campaign, type Step } from "@/lib/api";
import { useWorkspace } from "@/lib/workspace";
import { SkeletonRows } from "@/components/Skeleton";

/** The sequence: one row per message, each with its own angle and wait. */
export function StepsEditor({ campaign }: { campaign: Campaign }) {
  const { can, toast } = useWorkspace();
  const [steps, setSteps] = useState<Step[] | null>(null);
  const [dirty, setDirty] = useState(false);
  const [busy, setBusy] = useState(false);
  const locked = campaign.launch_status !== "not_launched" || !can("member");

  useEffect(() => {
    api<Step[]>(`/v1/campaigns/${campaign.id}/steps`).then(setSteps).catch((e) => toast(errorMessage(e), "error"));
  }, [campaign.id, toast]);

  function change(index: number, patch: Partial<Step>) {
    setSteps((current) => current && current.map((s, i) => (i === index ? { ...s, ...patch } : s)));
    setDirty(true);
  }

  async function save() {
    if (!steps) return;
    setBusy(true);
    try {
      setSteps(await api<Step[]>(`/v1/campaigns/${campaign.id}/steps`, { method: "PUT", body: JSON.stringify({ steps }) }));
      setDirty(false);
      toast("Sequence saved");
    } catch (e) {
      toast(errorMessage(e), "error");
    } finally {
      setBusy(false);
    }
  }

  if (!steps) return <SkeletonRows rows={5} />;
  let day = 0;
  return (
    <div>
      <ol className="sequence">
        {steps.map((s, i) => {
          day += s.delay_days;
          return (
            <li key={s.step} className="sequence-step">
              <div className="sequence-when">
                <span className="row-name">Step {s.step}</span>
                <span className="row-desc">{i === 0 ? "Day 1" : `Day ${day + 1}`}</span>
              </div>
              <div className="sequence-body">
                <label className="sr-only" htmlFor={`angle-${s.step}`}>
                  Angle for step {s.step}
                </label>
                <textarea
                  id={`angle-${s.step}`}
                  className="input textarea"
                  rows={2}
                  maxLength={500}
                  value={s.angle}
                  disabled={locked}
                  onChange={(e) => change(i, { angle: e.target.value })}
                />
                {i > 0 && (
                  <div className="inline-form">
                    <label className="muted small" htmlFor={`delay-${s.step}`}>
                      Wait
                    </label>
                    <input
                      id={`delay-${s.step}`}
                      type="number"
                      className="input input-date"
                      min={1}
                      max={60}
                      value={s.delay_days}
                      disabled={locked}
                      onChange={(e) => change(i, { delay_days: Math.max(1, Number(e.target.value)) })}
                      style={{ width: 80 }}
                    />
                    <span className="muted small">days after step {s.step - 1}</span>
                    {!locked && i === steps.length - 1 && (
                      <button
                        type="button"
                        className="btn btn-quiet btn-danger"
                        onClick={() => {
                          setSteps(steps.slice(0, -1));
                          setDirty(true);
                        }}
                      >
                        Remove step
                      </button>
                    )}
                  </div>
                )}
              </div>
            </li>
          );
        })}
      </ol>
      {!locked && (
        <div className="inline-form" style={{ marginTop: 16 }}>
          {steps.length < 10 && (
            <button
              type="button"
              className="btn"
              onClick={() => {
                setSteps([...steps, { step: steps.length + 1, delay_days: 4, angle: "Follow-up: a new, relevant reason to reply." }]);
                setDirty(true);
              }}
            >
              Add a step
            </button>
          )}
          <button type="button" className="btn btn-primary" disabled={!dirty || busy} onClick={() => void save()}>
            {busy ? "Saving…" : "Save sequence"}
          </button>
        </div>
      )}
      <p className="muted small" style={{ marginTop: 14 }}>
        The angle tells Gazi what each message should do. It writes the actual words for every buyer separately. Only
        the first message may contain a link; follow-ups offer resources in words.
      </p>
    </div>
  );
}
