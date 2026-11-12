import BixLogo from "../components/BixLogo";
import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { useAuth } from "../context/AuthContext";

export default function Login() {
  const { login } = useAuth();
  const navigate = useNavigate();

  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [otp, setOtp] = useState("");
  const [needOtp, setNeedOtp] = useState(false);
  const [error, setError] = useState(null);
  const [loading, setLoading] = useState(false);

  const handleSubmit = async (e) => {
    e.preventDefault();
    setError(null);
    setLoading(true);
    try {
      await login(email, password, needOtp ? otp : undefined);
      navigate("/", { replace: true });
    } catch (err) {
      const detail = err.response?.data?.detail;
      if (detail === "totp_required") {
        setNeedOtp(true);
      } else {
        setError(detail || "Login failed. Check your credentials.");
      }
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="flex h-screen w-screen items-center justify-center bg-slate-900">
      <div className="w-full max-w-sm rounded-2xl bg-white p-8 shadow-xl">
        <div className="mb-6 flex justify-center">
          <BixLogo size="lg" caption="Business intelligence · by Ghaf" />
        </div>

        <form onSubmit={handleSubmit} className="space-y-4">
          <div>
            <label className="mb-1 block text-sm font-medium text-slate-700">Email</label>
            <input
              type="email"
              required
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              placeholder="you@clinic.ae"
              className="w-full rounded-lg border border-slate-300 px-3 py-2 text-sm focus:border-ghaf-500 focus:outline-none focus:ring-1 focus:ring-ghaf-500"
            />
          </div>
          <div>
            <label className="mb-1 block text-sm font-medium text-slate-700">Password</label>
            <input
              type="password"
              required
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              className="w-full rounded-lg border border-slate-300 px-3 py-2 text-sm focus:border-ghaf-500 focus:outline-none focus:ring-1 focus:ring-ghaf-500"
            />
          </div>

          {needOtp && (
            <div>
              <label className="mb-1 block text-sm font-medium text-slate-700">Authenticator code</label>
              <input
                type="text"
                inputMode="numeric"
                autoComplete="one-time-code"
                autoFocus
                required
                maxLength={6}
                value={otp}
                onChange={(e) => setOtp(e.target.value.replace(/\D/g, ""))}
                placeholder="6-digit code"
                className="w-full rounded-lg border border-slate-300 px-3 py-2 text-center text-lg tracking-widest focus:border-ghaf-500 focus:outline-none focus:ring-1 focus:ring-ghaf-500"
              />
              <p className="mt-1 text-xs text-slate-500">Open your authenticator app and enter the code for Ghaf Bix.</p>
            </div>
          )}

          {error && <p className="text-sm text-red-600">{error}</p>}

          <button
            type="submit"
            disabled={loading}
            className="w-full rounded-lg bg-ghaf-600 px-4 py-2 text-sm font-semibold text-white hover:bg-ghaf-700 disabled:opacity-60"
          >
            {loading ? "Signing in…" : needOtp ? "Verify" : "Sign in"}
          </button>
        </form>
      </div>
    </div>
  );
}
