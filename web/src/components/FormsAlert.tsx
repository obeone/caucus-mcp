/**
 * FormsAlert — transient header badge for pending operator forms.
 *
 * Renders nothing when there are no pending forms.
 * When pending forms exist, shows an amber badge button showing the count.
 * Clicking the badge opens a Dialog containing the full pending-forms list.
 * Clicking a form in that list opens the answer/reject FormModal (reused
 * from FormsPanel).
 */

import { useState, useCallback } from "react";
import { useDashStore } from "../store/wsStore";
import { useToast } from "./ToastProvider";
import { FormModal, FormRow } from "./FormsPanel";
import type { FormObj } from "../store/types";
import { FileQuestion, X } from "lucide-react";
import * as Dialog from "@radix-ui/react-dialog";
import { cn } from "../lib/utils";

export default function FormsAlert() {
  const forms = useDashStore((s) => s.forms);
  const role = useDashStore((s) => s.role);
  const sendAnswer = useDashStore((s) => s.sendAnswer);
  const sendCancelForm = useDashStore((s) => s.sendCancelForm);
  const { toast } = useToast();

  const [listOpen, setListOpen] = useState(false);
  const [openForm, setOpenForm] = useState<FormObj | null>(null);

  // Hooks must be declared unconditionally before any early return.
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

  const pendingForms = forms.filter((f) => f.status === "pending");

  // Nothing to show when no forms are pending.
  if (pendingForms.length === 0) return null;

  return (
    <>
      {/* Badge button in the header */}
      <button
        onClick={() => setListOpen(true)}
        className={cn(
          "flex items-center gap-1.5 px-2.5 py-1 rounded-sm border transition-all",
          "border-amber/60 text-amber bg-amber/8 hover:bg-amber/15",
          "font-chrome font-bold tracking-[2px] text-[10px] uppercase",
          "shadow-[0_0_12px_-5px_#ffb22e] animate-blink"
        )}
        aria-label={`Open pending forms (${pendingForms.length})`}
        title="Pending operator forms"
      >
        <FileQuestion size={11} aria-hidden="true" />
        Forms {pendingForms.length}
      </button>

      {/* Pending-forms list Dialog */}
      <Dialog.Root open={listOpen} onOpenChange={setListOpen}>
        <Dialog.Portal>
          <Dialog.Overlay className="fixed inset-0 bg-bg/80 backdrop-blur-sm z-50 animate-fade-in" />
          <Dialog.Content
            className={cn(
              "fixed left-1/2 top-1/2 -translate-x-1/2 -translate-y-1/2 z-50",
              "w-[min(480px,92vw)] max-h-[70vh] flex flex-col",
              "bg-panel border border-amber/40 border-t-[3px] border-t-amber rounded-b-sm shadow-2xl shadow-black/50",
              "animate-wizard-in"
            )}
            aria-describedby="forms-alert-desc"
          >
            {/* Header */}
            <div className="flex items-center gap-2 px-5 py-4 border-b border-line flex-shrink-0">
              <FileQuestion size={14} className="text-amber" aria-hidden="true" />
              <Dialog.Title className="font-chrome font-bold tracking-wide text-sm text-ink flex-1">
                Pending Forms
              </Dialog.Title>
              <p id="forms-alert-desc" className="sr-only">
                List of pending operator forms awaiting your response.
              </p>
              <span className="text-[10px] font-mono text-amber border border-amber/40 px-1.5 py-0.5 rounded-sm">
                {pendingForms.length} pending
              </span>
              <Dialog.Close asChild>
                <button
                  className="text-dim hover:text-ink transition-colors ml-1"
                  aria-label="Close forms list"
                >
                  <X size={14} />
                </button>
              </Dialog.Close>
            </div>

            {/* Forms list */}
            <div
              className="flex-1 overflow-y-auto"
              role="list"
              aria-label="Pending forms"
            >
              {pendingForms.map((f) => (
                <div key={f.id} role="listitem">
                  <FormRow
                    form={f}
                    onOpen={(form) => {
                      setListOpen(false);
                      setOpenForm(form);
                    }}
                  />
                </div>
              ))}
            </div>
          </Dialog.Content>
        </Dialog.Portal>
      </Dialog.Root>

      {/* Per-form answer/reject modal */}
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
    </>
  );
}
