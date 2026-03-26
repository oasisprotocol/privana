const contextElement = document.getElementById("auth-context");
const context = JSON.parse(contextElement.textContent);

const appNameElement = document.getElementById("app-name");
const redirectOriginElement = document.getElementById("redirect-origin");
const walletAddressElement = document.getElementById("wallet-address");
const statusElement = document.getElementById("status-message");
const errorElement = document.getElementById("error-message");
const connectButton = document.getElementById("connect-button");
const cancelButton = document.getElementById("cancel-button");

appNameElement.textContent = context.displayName;
redirectOriginElement.textContent = context.redirectOrigin;

function showStatus(message) {
  statusElement.textContent = message;
}

function showError(message) {
  errorElement.hidden = false;
  errorElement.textContent = message;
}

function clearError() {
  errorElement.hidden = true;
  errorElement.textContent = "";
}

function setBusy(isBusy) {
  connectButton.disabled = isBusy;
  cancelButton.disabled = isBusy;
}

function buildSiweMessage({ domain, address, uri, chainId, nonce, issuedAt, expirationTime }) {
  return `${domain} wants you to sign in with your Ethereum account:
${address}

Sign in to Flexvaults

URI: ${uri}
Version: 1
Chain ID: ${chainId}
Nonce: ${nonce}
Issued At: ${issuedAt}
Expiration Time: ${expirationTime}`;
}

function buildRedirectUrl(params) {
  const url = new URL(context.redirectUri);
  Object.entries(params).forEach(([key, value]) => {
    if (value) {
      url.searchParams.set(key, value);
    }
  });
  return url.toString();
}

function deliverSuccess(code) {
  if (context.responseMode === "web_message" && window.opener && !window.opener.closed) {
    window.opener.postMessage(
      {
        type: "flexvaults-auth-response",
        code,
        state: context.state,
      },
      context.redirectOrigin,
    );
    window.close();
    return;
  }

  window.location.assign(buildRedirectUrl({ code, state: context.state }));
}

function deliverError(error, errorDescription) {
  if (context.responseMode === "web_message" && window.opener && !window.opener.closed) {
    window.opener.postMessage(
      {
        type: "flexvaults-auth-response",
        error,
        error_description: errorDescription,
        state: context.state,
      },
      context.redirectOrigin,
    );
    window.close();
    return;
  }

  window.location.assign(
    buildRedirectUrl({
      error,
      error_description: errorDescription,
      state: context.state,
    }),
  );
}

async function fetchJson(url, options = {}) {
  const response = await fetch(url, {
    credentials: "same-origin",
    ...options,
    headers: {
      "Content-Type": "application/json",
      ...(options.headers || {}),
    },
  });

  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    const detail = data.error_description || data.detail || "Request failed";
    throw new Error(detail);
  }
  return data;
}

async function startLogin() {
  clearError();

  if (!window.ethereum) {
    showError("No Ethereum wallet detected. Install MetaMask or another EIP-1193 wallet.");
    return;
  }

  if (context.siweOrigin !== window.location.origin) {
    showError(
      `Auth page misconfigured: expected auth origin ${context.siweOrigin}, actual origin ${window.location.origin}.`,
    );
    return;
  }

  setBusy(true);

  try {
    showStatus("Connecting wallet…");
    const accounts = await window.ethereum.request({ method: "eth_requestAccounts" });
    const address = accounts?.[0];
    if (!address) {
      throw new Error("Wallet did not return an account.");
    }

    walletAddressElement.textContent = address;

    showStatus("Preparing sign-in message…");
    const chainIdHex = await window.ethereum.request({ method: "eth_chainId" });
    const chainId = Number.parseInt(chainIdHex, 16);
    const nonceResponse = await fetchJson(
      `${context.nonceEndpoint}?address=${encodeURIComponent(address)}`,
      { method: "GET", headers: {} },
    );
    const siweAddress = nonceResponse.address || address;
    walletAddressElement.textContent = siweAddress;

    const issuedAt = new Date().toISOString();
    const expirationTime = new Date(
      Date.now() + context.authTokenValiditySeconds * 1000,
    ).toISOString();
    const siweMessage = buildSiweMessage({
      domain: context.siweDomain,
      address: siweAddress,
      uri: window.location.origin,
      chainId,
      nonce: nonceResponse.nonce,
      issuedAt,
      expirationTime,
    });

    showStatus("Requesting wallet signature…");
    const signature = await window.ethereum.request({
      method: "personal_sign",
      params: [siweMessage, siweAddress],
    });

    showStatus("Finalizing sign-in…");
    const response = await fetchJson(context.authorizeEndpoint, {
      method: "POST",
      body: JSON.stringify({
        siwe_message: siweMessage,
        signature,
        client_id: context.clientId,
        redirect_uri: context.redirectUri,
        code_challenge: context.codeChallenge,
        code_challenge_method: context.codeChallengeMethod,
      }),
    });

    deliverSuccess(response.code);
  } catch (error) {
    console.error(error);
    const message = error instanceof Error ? error.message : "Sign-in failed";
    showStatus("Unable to complete sign-in.");
    showError(message);
    setBusy(false);
  }
}

connectButton.addEventListener("click", () => {
  void startLogin();
});

cancelButton.addEventListener("click", () => {
  deliverError("access_denied", "User cancelled sign-in");
});
