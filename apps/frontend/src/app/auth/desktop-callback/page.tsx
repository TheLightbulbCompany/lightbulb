"use client";

import { useAuth, SignIn } from "@clerk/nextjs";
import { useEffect, useState } from "react";

import { DEEP_LINK_SCHEME } from "@/lib/domain";

const API_URL = process.env.NEXT_PUBLIC_API_URL || "https://api-dev.thelightbulb.company/api/v1";

export default function DesktopCallback() {
  const { isLoaded, isSignedIn, getToken } = useAuth();
  const [status, setStatus] = useState("Signing in...");

  useEffect(() => {
    if (!isLoaded || !isSignedIn) return;

    async function getSignInToken() {
      try {
        // Get a Clerk JWT for authenticating with our backend
        const jwt = await getToken();
        if (!jwt) return;

        setStatus("Preparing desktop sign-in...");

        // Ask our backend to create a one-time Clerk sign-in token
        const resp = await fetch(`${API_URL}/auth/desktop/sign-in-token`, {
          method: "POST",
          headers: {
            Authorization: `Bearer ${jwt}`,
            "Content-Type": "application/json",
          },
        });

        if (!resp.ok) {
          setStatus("Failed to create sign-in token. Please try again.");
          return;
        }

        const data = await resp.json();

        // Redirect to the desktop app with the sign-in token
        setStatus("Opening Lightbulb desktop app...");
        window.location.href = `${DEEP_LINK_SCHEME}://auth?ticket=${encodeURIComponent(data.token)}`;
      } catch (err) {
        console.error("Desktop callback error:", err);
        setStatus("Something went wrong. Please try again.");
      }
    }

    getSignInToken();
  }, [isLoaded, isSignedIn, getToken]);

  if (isLoaded && !isSignedIn) {
    return (
      <div className="flex justify-center items-center h-screen">
        <SignIn
          forceRedirectUrl="/auth/desktop-callback"
          appearance={{ elements: { rootBox: "mx-auto" } }}
        />
      </div>
    );
  }

  return (
    <div className="flex justify-center items-center h-screen">
      <div className="text-center">
        <h1 className="text-xl font-semibold mb-2">{status}</h1>
        <p className="text-sm text-muted-foreground">
          You can close this tab after the app opens.
        </p>
      </div>
    </div>
  );
}
