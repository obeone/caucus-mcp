/**
 * FormsPanel — pending operator forms.
 *
 * Shows a badge/count of pending forms. Clicking a form opens a modal with:
 * - Read-only view of the questionnaire (title, fields, audience, status)
 * - Fill-and-submit workflow (answer command)
 * - Reject with reason (cancel_form command)
 */

import { useState, useCallback } from "react";
import { useDashStore } from "../store/wsStore";
import { cn } from "../lib/utils";
import { useToast } from "./ToastProvider";
import type { FormObj, FormField } from "../store/types";
import * as Dialog from "@radix-ui/react-dialog";
import { X, FileQuestion, CheckCircle, XCircle, Clock } from "lucide-react";

// ── Form answer modal ────────────────────────────────────────────────────────

interface AnswerValues {
  [key: string]: string | string[];
}

interface FormModalProps {
  form: FormObj;
  open: boolean;
  onClose: () => void;
  onAnswer: (id: string, answers: AnswerValues) => void;
  onCancel: (id: string, reason: string) => void;
  role: "operator" | "observer";
}

interface FieldInputProps {
  field: FormField;
  value: string | string[];
  onChange: (v: string | string[]) => void;
}

/** Shared option-row chrome for radio/checkbox choices and their "Other" row. */
const OPTION_ROW =
  "flex items-center gap-2.5 px-3 py-2 rounded-sm border cursor-pointer transition-all bg-panel-2";

/**
 * Single-choice field. When `allow_other` is set the asker explicitly permits a
 * value outside `options`, so we render an extra "Other…" radio that reveals a
 * free-text input; the typed string becomes the answer. `otherMode` is local
 * state (not derivable from `value` alone) so picking "Other" stays sticky even
 * before any text is typed — required validation then sees the empty value.
 */
function RadioField({ field, value, onChange }: FieldInputProps) {
  const opts = field.options ?? [];
  const valueStr = typeof value === "string" ? value : "";
  const [otherMode, setOtherMode] = useState(
    field.allow_other === true && valueStr !== "" && !opts.includes(valueStr)
  );

  return (
    <div className="flex flex-col gap-2" role="radiogroup" aria-label={field.label}>
      {opts.map((opt) => {
        const checked = !otherMode && valueStr === opt;
        return (
          <label
            key={opt}
            className={cn(
              OPTION_ROW,
              checked ? "border-amber bg-amber/8" : "border-line hover:border-amber/50"
            )}
          >
            <input
              type="radio"
              name={field.key}
              value={opt}
              checked={checked}
              onChange={() => {
                setOtherMode(false);
                onChange(opt);
              }}
              className="accent-amber w-3.5 h-3.5"
            />
            <span className="text-xs font-mono text-ink">{opt}</span>
          </label>
        );
      })}
      {field.allow_other && (
        <>
          <label
            className={cn(
              OPTION_ROW,
              otherMode ? "border-amber bg-amber/8" : "border-line hover:border-amber/50"
            )}
          >
            <input
              type="radio"
              name={field.key}
              checked={otherMode}
              onChange={() => {
                setOtherMode(true);
                onChange("");
              }}
              className="accent-amber w-3.5 h-3.5"
              aria-label={`${field.label} — other`}
            />
            <span className="text-xs font-mono text-dim italic">Other…</span>
          </label>
          {otherMode && (
            <input
              type="text"
              value={valueStr}
              onChange={(e) => onChange(e.target.value)}
              placeholder="Type a custom value…"
              className="ml-6 w-[calc(100%-1.5rem)] bg-bg text-ink border border-line rounded-sm px-3 py-2 text-xs font-mono focus:outline-none focus:border-cyan"
              aria-label={`${field.label} — custom value`}
            />
          )}
        </>
      )}
    </div>
  );
}

/**
 * Multi-choice field. Listed selections live in `value`; when `allow_other` is
 * set, an "Other…" checkbox reveals a free-text input whose trimmed value is
 * appended as one extra entry. `listed` is derived from `value` each render so
 * checkbox state never desyncs; the custom entry is tracked in local state.
 */
function CheckboxField({ field, value, onChange }: FieldInputProps) {
  const opts = field.options ?? [];
  const selected = Array.isArray(value) ? value : [];
  const listed = selected.filter((v) => opts.includes(v));
  const customExisting = selected.find((v) => !opts.includes(v)) ?? "";
  const [otherMode, setOtherMode] = useState(
    field.allow_other === true && customExisting !== ""
  );
  const [otherText, setOtherText] = useState(customExisting);

  // Re-emit the full answer array: listed selections plus the custom entry when
  // "Other" is active and non-blank.
  const emit = (nextListed: string[], on: boolean, text: string) => {
    const arr = [...nextListed];
    if (on && text.trim()) arr.push(text.trim());
    onChange(arr);
  };

  return (
    <div className="flex flex-col gap-2" role="group" aria-label={field.label}>
      {opts.map((opt) => {
        const checked = listed.includes(opt);
        return (
          <label
            key={opt}
            className={cn(
              OPTION_ROW,
              checked ? "border-amber bg-amber/8" : "border-line hover:border-amber/50"
            )}
          >
            <input
              type="checkbox"
              value={opt}
              checked={checked}
              onChange={(e) =>
                emit(
                  e.target.checked
                    ? [...listed, opt]
                    : listed.filter((v) => v !== opt),
                  otherMode,
                  otherText
                )
              }
              className="accent-amber w-3.5 h-3.5"
            />
            <span className="text-xs font-mono text-ink">{opt}</span>
          </label>
        );
      })}
      {field.allow_other && (
        <>
          <label
            className={cn(
              OPTION_ROW,
              otherMode ? "border-amber bg-amber/8" : "border-line hover:border-amber/50"
            )}
          >
            <input
              type="checkbox"
              checked={otherMode}
              onChange={(e) => {
                setOtherMode(e.target.checked);
                emit(listed, e.target.checked, otherText);
              }}
              className="accent-amber w-3.5 h-3.5"
              aria-label={`${field.label} — other`}
            />
            <span className="text-xs font-mono text-dim italic">Other…</span>
          </label>
          {otherMode && (
            <input
              type="text"
              value={otherText}
              onChange={(e) => {
                setOtherText(e.target.value);
                emit(listed, true, e.target.value);
              }}
              placeholder="Type a custom value…"
              className="ml-6 w-[calc(100%-1.5rem)] bg-bg text-ink border border-line rounded-sm px-3 py-2 text-xs font-mono focus:outline-none focus:border-cyan"
              aria-label={`${field.label} — custom value`}
            />
          )}
        </>
      )}
    </div>
  );
}

/** Dispatch a form field to its input widget by type. */
function FieldInput({ field, value, onChange }: FieldInputProps) {
  if (field.type === "text") {
    return (
      <input
        type="text"
        value={value as string}
        onChange={(e) => onChange(e.target.value)}
        required={field.required}
        className="w-full bg-bg text-ink border border-line rounded-sm px-3 py-2 text-xs font-mono focus:outline-none focus:border-cyan"
        aria-label={field.label}
      />
    );
  }

  if (field.type === "textarea") {
    return (
      <textarea
        value={value as string}
        onChange={(e) => onChange(e.target.value)}
        required={field.required}
        rows={3}
        className="w-full bg-bg text-ink border border-line rounded-sm px-3 py-2 text-xs font-mono focus:outline-none focus:border-cyan resize-y"
        aria-label={field.label}
      />
    );
  }

  if (field.type === "radio") {
    return <RadioField field={field} value={value} onChange={onChange} />;
  }

  if (field.type === "checkbox") {
    return <CheckboxField field={field} value={value} onChange={onChange} />;
  }

  return null;
}

/** Answer/reject modal for a single form. Exported for reuse in FormsAlert. */
export function FormModal({ form, open, onClose, onAnswer, onCancel, role }: FormModalProps) {
  const [answers, setAnswers] = useState<AnswerValues>(() => {
    const init: AnswerValues = {};
    for (const f of form.fields) {
      init[f.key] = f.type === "checkbox" ? [] : "";
    }
    return init;
  });
  const [rejectReason, setRejectReason] = useState("");
  const [showReject, setShowReject] = useState(false);
  const [errors, setErrors] = useState<Record<string, string>>({});

  const isPending = form.status === "pending";
  const isOperator = role === "operator";

  function validate(): boolean {
    const errs: Record<string, string> = {};
    for (const f of form.fields) {
      if (!f.required) continue;
      const v = answers[f.key];
      const empty = Array.isArray(v) ? v.length === 0 : !v;
      if (empty) errs[f.key] = "Required";
    }
    setErrors(errs);
    return Object.keys(errs).length === 0;
  }

  function handleSubmit() {
    if (!validate()) return;
    onAnswer(form.id, answers);
    onClose();
  }

  function handleReject() {
    onCancel(form.id, rejectReason);
    onClose();
  }

  return (
    <Dialog.Root open={open} onOpenChange={(v) => !v && onClose()}>
      <Dialog.Portal>
        <Dialog.Overlay className="fixed inset-0 bg-bg/85 backdrop-blur-sm z-50 animate-fade-in" />
        <Dialog.Content
          className="fixed left-1/2 top-1/2 -translate-x-1/2 -translate-y-1/2 z-50 w-[min(560px,92vw)] bg-panel border border-amber border-t-[3px] rounded-b-sm shadow-2xl shadow-black/50 animate-wizard-in flex flex-col max-h-[85vh]"
          aria-describedby="form-modal-desc"
        >
          {/* Header */}
          <div className="flex flex-col gap-1.5 px-5 py-4 border-b border-line flex-shrink-0">
            <div className="flex items-start gap-2">
              <Dialog.Title className="font-chrome font-bold tracking-wide text-[15px] text-ink flex-1">
                {form.title}
              </Dialog.Title>
              <Dialog.Close asChild>
                <button
                  className="text-dim hover:text-ink transition-colors flex-shrink-0"
                  aria-label="Close form"
                >
                  <X size={15} />
                </button>
              </Dialog.Close>
            </div>
            <p id="form-modal-desc" className="text-[10px] font-mono text-dim">
              from{" "}
              <span className="text-ink">{form.asker}</span> → audience:{" "}
              <span className="text-ink">{form.audience}</span>
            </p>
            {/* Status badge */}
            <div>
              {form.status === "pending" && (
                <span className="inline-flex items-center gap-1 text-[10px] font-mono text-amber border border-amber/40 px-2 py-0.5 rounded-sm">
                  <Clock size={9} /> pending
                </span>
              )}
              {form.status === "answered" && (
                <span className="inline-flex items-center gap-1 text-[10px] font-mono text-green border border-green/40 px-2 py-0.5 rounded-sm">
                  <CheckCircle size={9} /> answered
                </span>
              )}
              {form.status === "cancelled" && (
                <span className="inline-flex items-center gap-1 text-[10px] font-mono text-red border border-red/40 px-2 py-0.5 rounded-sm">
                  <XCircle size={9} /> cancelled
                </span>
              )}
            </div>
          </div>

          {/* Fields */}
          <div className="flex-1 overflow-y-auto px-5 py-4 flex flex-col gap-5">
            {form.fields.map((field) => (
              <div key={field.key} className="flex flex-col gap-2">
                <label className="font-chrome font-bold tracking-wide text-[13px] text-ink flex items-center gap-2">
                  {field.label}
                  {field.required && (
                    <span className="text-[9px] font-mono text-amber border border-amber/30 bg-amber/10 px-1.5 py-0.5 rounded-sm">
                      required
                    </span>
                  )}
                </label>
                {isPending && isOperator ? (
                  <>
                    <FieldInput
                      field={field}
                      value={answers[field.key] ?? ""}
                      onChange={(v) =>
                        setAnswers((prev) => ({ ...prev, [field.key]: v }))
                      }
                    />
                    {errors[field.key] && (
                      <p className="text-[11px] font-mono text-red">
                        {errors[field.key]}
                      </p>
                    )}
                  </>
                ) : (
                  /* Read-only view for non-operator or already resolved */
                  <div className="text-xs font-mono text-dim italic px-3 py-2 border border-line rounded-sm bg-panel-2">
                    {field.type === "radio" || field.type === "checkbox"
                      ? (field.options ?? []).map((o) => (
                          <div key={o} className="flex items-center gap-2 py-0.5">
                            <span className="w-3 h-3 border border-line rounded-sm flex-shrink-0" />
                            {o}
                          </div>
                        ))
                      : `(${field.type} input)`}
                  </div>
                )}
              </div>
            ))}

            {/* Reject reason */}
            {showReject && (
              <div className="flex flex-col gap-2 border-t border-line pt-4">
                <label className="font-chrome font-bold tracking-wide text-[12px] text-red">
                  Rejection reason (optional)
                </label>
                <textarea
                  value={rejectReason}
                  onChange={(e) => setRejectReason(e.target.value)}
                  rows={2}
                  placeholder="Explain why you are cancelling this form…"
                  className="w-full bg-bg text-ink border border-red/40 rounded-sm px-3 py-2 text-xs font-mono focus:outline-none focus:border-red resize-none"
                  aria-label="Rejection reason"
                />
              </div>
            )}
          </div>

          {/* Footer */}
          {isPending && isOperator && (
            <div className="flex items-center gap-2 px-5 py-3.5 border-t border-line flex-shrink-0">
              {!showReject ? (
                <button
                  onClick={() => setShowReject(true)}
                  className="font-chrome font-bold tracking-widest text-[10px] uppercase px-3 py-1.5 border border-red rounded-sm text-red hover:bg-red hover:text-bg transition-all"
                >
                  Reject
                </button>
              ) : (
                <>
                  <button
                    onClick={() => setShowReject(false)}
                    className="font-chrome font-bold tracking-widest text-[10px] uppercase px-3 py-1.5 border border-line rounded-sm text-dim hover:border-ink hover:text-ink transition-all"
                  >
                    Back
                  </button>
                  <button
                    onClick={handleReject}
                    className="font-chrome font-bold tracking-widest text-[10px] uppercase px-3 py-1.5 border border-red rounded-sm text-red hover:bg-red hover:text-bg transition-all"
                  >
                    Confirm reject
                  </button>
                </>
              )}
              <div className="flex-1" />
              <button
                onClick={handleSubmit}
                className="font-chrome font-bold tracking-widest text-[10px] uppercase px-4 py-1.5 bg-amber border border-amber rounded-sm text-bg hover:bg-amber/80 transition-all shadow-[0_0_14px_-5px_#ffb22e]"
              >
                Submit answer
              </button>
            </div>
          )}
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}

// ── Form row in the list ─────────────────────────────────────────────────────

/** Single form row in the forms list. Exported for reuse in FormsAlert. */
export function FormRow({
  form,
  onOpen,
}: {
  form: FormObj;
  onOpen: (form: FormObj) => void;
}) {
  const isPending = form.status === "pending";

  return (
    <div
      className={cn(
        "flex items-center gap-3 p-3 border-l-[3px] border-b border-line/50 cursor-pointer hover:bg-panel transition-all",
        isPending ? "border-l-amber bg-amber/3" : "border-l-line bg-transparent"
      )}
      role="button"
      tabIndex={0}
      onClick={() => onOpen(form)}
      onKeyDown={(e) => e.key === "Enter" && onOpen(form)}
      aria-label={`Form: ${form.title} — ${form.status}`}
    >
      <FileQuestion
        size={14}
        className={isPending ? "text-amber" : "text-dim"}
        aria-hidden="true"
      />
      <div className="flex-1 min-w-0">
        <p className="font-chrome font-bold tracking-wide text-[12px] text-ink truncate">
          {form.title}
        </p>
        <p className="text-[10px] font-mono text-dim truncate">
          {form.asker} → {form.audience} · {form.fields.length} field(s)
        </p>
      </div>
      {isPending && (
        <span className="text-[10px] font-mono text-amber border border-amber/40 px-2 py-0.5 rounded-sm flex-shrink-0 animate-blink">
          pending
        </span>
      )}
      {form.status === "answered" && (
        <CheckCircle size={13} className="text-green flex-shrink-0" />
      )}
      {form.status === "cancelled" && (
        <XCircle size={13} className="text-red flex-shrink-0" />
      )}
    </div>
  );
}

// ── Panel ─────────────────────────────────────────────────────────────────────

export default function FormsPanel() {
  const forms = useDashStore((s) => s.forms);
  const role = useDashStore((s) => s.role);
  const sendAnswer = useDashStore((s) => s.sendAnswer);
  const sendCancelForm = useDashStore((s) => s.sendCancelForm);
  const { toast } = useToast();

  const [openForm, setOpenForm] = useState<FormObj | null>(null);

  const pending = forms.filter((f) => f.status === "pending");
  const resolved = forms.filter((f) => f.status !== "pending");

  const handleAnswer = useCallback(
    (id: string, answers: Record<string, string | string[]>) => {
      sendAnswer(id, answers);
      toast({ title: "Answer submitted", variant: "success" });
    },
    [sendAnswer, toast]
  );

  const handleCancel = useCallback(
    (id: string, reason: string) => {
      sendCancelForm(id, reason);
      toast({ title: "Form rejected", variant: "default" });
    },
    [sendCancelForm, toast]
  );

  return (
    <div className="h-full flex flex-col overflow-hidden">
      {/* Stats / badge bar */}
      <div className="flex items-center gap-4 px-5 py-2.5 border-b border-line bg-panel text-[11px] font-mono flex-shrink-0">
        {pending.length > 0 ? (
          <span className="flex items-center gap-2 text-amber font-semibold">
            <span className="w-2 h-2 rounded-full bg-amber animate-blink" />
            {pending.length} pending form(s)
          </span>
        ) : (
          <span className="text-dim">No pending forms</span>
        )}
        {resolved.length > 0 && (
          <span className="text-dim ml-auto">{resolved.length} resolved</span>
        )}
      </div>

      {/* Forms list */}
      <div
        className="flex-1 overflow-y-auto"
        role="list"
        aria-label="Operator forms"
      >
        {forms.length === 0 ? (
          <div className="flex flex-col items-center justify-center h-40 gap-3 text-dim">
            <FileQuestion size={28} className="opacity-30" />
            <span className="text-sm font-mono">— no forms —</span>
          </div>
        ) : (
          <>
            {pending.length > 0 && (
              <div>
                <div className="px-4 py-2 text-[10px] font-chrome font-bold tracking-[3px] text-amber uppercase border-b border-line/50 bg-amber/3">
                  Pending
                </div>
                {pending.map((f) => (
                  <div key={f.id} role="listitem">
                    <FormRow form={f} onOpen={setOpenForm} />
                  </div>
                ))}
              </div>
            )}
            {resolved.length > 0 && (
              <div>
                <div className="px-4 py-2 text-[10px] font-chrome font-bold tracking-[3px] text-dim uppercase border-b border-line/50">
                  Resolved
                </div>
                {resolved.map((f) => (
                  <div key={f.id} role="listitem">
                    <FormRow form={f} onOpen={setOpenForm} />
                  </div>
                ))}
              </div>
            )}
          </>
        )}
      </div>

      {/* Form detail modal */}
      {openForm && (
        <FormModal
          form={openForm}
          open={openForm !== null}
          onClose={() => setOpenForm(null)}
          onAnswer={handleAnswer}
          onCancel={handleCancel}
          role={role}
        />
      )}
    </div>
  );
}
