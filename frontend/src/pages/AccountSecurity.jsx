import { useState } from "react";
import apiClient from "../api/client";
import { useAuth } from "../context/AuthContext";

export default function AccountSecurity() {
  const { user, updateUser } = useAuth();
  const [setup, setSetup] = useState(null); // { qr_svg, secret } while enrolling
  const [code, setCode] = useState("");
  const [error, setError] = useState(null);
  const [message, setMessage] = useState(null);
  const [busy, setBusy] = useState(false);

  const enabled = !!user?.totp_enabled;

  const run = async (fn) => {
    setError(null);
    setMessage(null);
    setBusy(true);
    try {
      await fn();
    } catch (err) {
      setError(err.response?.data?.detail || "Something went wrong");
    } finally {
      setBusy(false);
    }
  };

  const start = () =>
    run(async () => {
      const { data } = await apiClient.post("/auth/totp/setup");
      setSetup(data);
      setCode("");
    });

  const confirm = (e) => {
    e.preventDefault();
    run(async () => {
      const { data } = await apiClient.post("/auth/totp/enable", { code });
      updateUser(data);
      setSetup(null);
      setCode("");
      setMessage("Authenticator app is on. You'll be asked for a code at every sign-in.");
    });
  };

  const disable = (e) => {
    e.preventDefault();
    run(async () => {
      const { data } = await apiClient.post("/auth/totp/disable", { code });
      updateUser(data);
      setCode("");
      setMessage("Authenticator app is off.");
    });
  };

  const codeInput = (
    <input
      type="text"
      inputMode="numeric"
      autoComplete="one-time-code"
      required
      maxLength={6}
      value={code}
      onChange={(e) => setCode(e.target.value.replace(/\D/g, ""))}
      placeholder="6-digit code"
      className="w-40 rounded-lg border border-slate-300 px-3 py-2 text-center tracking-widest focus:border-ghaf-500 focus:outline-none focus:ring-1 focus:ring-ghaf-500"
    />
  );
  const button = "rounded-lg bg-ghaf-600 px-4 py-2 text-sm font-semibold text-white hover:bg-ghaf-700 disabled:opacity-60";

  return (
    <div className="space-y-6">
      <div>
        <p className="gh-eyebrow">Account</p>
        <h1 className="gh-h1">Authenticator App</h1>
        <p className="text-sm text-slate-500">
          Add a 6-digit code from Google Authenticator, Microsoft Authenticator or similar to your sign-in.
        </p>
      </div>

      <div className="gh-card space-y-4 p-6">
        <p className="text-sm">
          Status:{" "}
          <span className={enabled ? "font-semibold text-green-700" : "font-semibold text-slate-600"}>
            {enabled ? "On" : "Off"}
          </span>
        </p>

        {!enabled && !setup && (
          <button onClick={start} disabled={busy} className={button}>
            Set up authenticator app
          </button>
        )}

        {!enabled && setup && (
          <form onSubmit={confirm} className="space-y-4">
            <p className="text-sm text-slate-700">1. Scan this QR code with your authenticator app.</p>
            <div className="h-48 w-48 bg-white p-2 [&>svg]:h-full [&>svg]:w-full" dangerouslySetInnerHTML={{ __html: setup.qr_svg }} />
            <p className="text-xs text-slate-500">
              Can't scan? Enter this key manually: <code className="select-all break-all font-mono">{setup.secret}</code>
            </p>
            <p className="text-sm text-slate-700">2. Enter the code the app shows to finish.</p>
            <div className="flex items-center gap-3">
              {codeInput}
              <button type="submit" disabled={busy || code.length !== 6} className={button}>
                Turn on
              </button>
            </div>
          </form>
        )}

        {enabled && (
          <form onSubmit={disable} className="space-y-3">
            <p className="text-sm text-slate-700">To turn it off, enter a current code from your app.</p>
            <div className="flex items-center gap-3">
              {codeInput}
              <button type="submit" disabled={busy || code.length !== 6} className={button}>
                Turn off
              </button>
            </div>
            <p className="text-xs text-slate-500">Lost your phone? An admin can reset it for you.</p>
          </form>
        )}

        {message && <p className="text-sm text-green-700">{message}</p>}
        {error && <p className="text-sm text-red-600">{error}</p>}
      </div>
    </div>
  );
}
