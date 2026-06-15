/**
 * ToastProvider
 *
 * Wraps the app in a Radix Toast viewport. Also exposes a module-level
 * `toast()` helper so any component can fire a notification without prop
 * drilling.
 */

import { createContext, useContext, useState, useCallback } from "react";
import * as Toast from "@radix-ui/react-toast";
import { cn } from "../lib/utils";
import { X } from "lucide-react";

interface ToastMessage {
  id: number;
  title: string;
  description?: string;
  variant?: "default" | "error" | "success";
}

interface ToastContextValue {
  toast: (msg: Omit<ToastMessage, "id">) => void;
}

const ToastContext = createContext<ToastContextValue>({ toast: () => {} });

export function useToast() {
  return useContext(ToastContext);
}

let _externalToast: ((msg: Omit<ToastMessage, "id">) => void) | null = null;

/** Fire a toast from outside React (e.g. store event handlers). */
export function fireToast(msg: Omit<ToastMessage, "id">) {
  _externalToast?.(msg);
}

let seq = 0;

export default function ToastProvider({ children }: { children: React.ReactNode }) {
  const [toasts, setToasts] = useState<ToastMessage[]>([]);

  const toast = useCallback((msg: Omit<ToastMessage, "id">) => {
    const id = ++seq;
    setToasts((prev) => [...prev, { ...msg, id }]);
    // Auto-remove after 4s
    setTimeout(() => {
      setToasts((prev) => prev.filter((t) => t.id !== id));
    }, 4_000);
  }, []);

  // Expose externally
  _externalToast = toast;

  return (
    <ToastContext.Provider value={{ toast }}>
      <Toast.Provider swipeDirection="right">
        {children}

        {toasts.map((t) => (
          <Toast.Root
            key={t.id}
            open
            onOpenChange={(open) => {
              if (!open) setToasts((prev) => prev.filter((x) => x.id !== t.id));
            }}
            className={cn(
              "flex items-start gap-3 p-4 rounded-sm border shadow-lg",
              "bg-panel-2 border-line text-ink",
              "data-[state=open]:animate-fade-in",
              t.variant === "error" && "border-red/40 bg-red/5",
              t.variant === "success" && "border-green/40 bg-green/5"
            )}
          >
            <div className="flex-1 min-w-0">
              <Toast.Title className="text-xs font-mono font-semibold tracking-wide">
                {t.title}
              </Toast.Title>
              {t.description && (
                <Toast.Description className="text-[11px] font-mono text-dim mt-1">
                  {t.description}
                </Toast.Description>
              )}
            </div>
            <Toast.Close asChild>
              <button
                className="text-dim hover:text-ink transition-colors flex-shrink-0"
                aria-label="Dismiss notification"
              >
                <X size={13} />
              </button>
            </Toast.Close>
          </Toast.Root>
        ))}

        <Toast.Viewport className="fixed bottom-4 right-4 flex flex-col gap-2 w-80 z-[9999]" />
      </Toast.Provider>
    </ToastContext.Provider>
  );
}
