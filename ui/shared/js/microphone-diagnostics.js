'use strict';

// Browser media failures share broad error names. Turn them into concise,
// actionable guidance without attempting to change browser or OS permissions.

async function _permissionState(name) {
  try {
    const result = await navigator.permissions?.query?.({ name });
    return result?.state || '';
  } catch (_) {
    return '';
  }
}

export async function microphonePermissionState() {
  return _permissionState('microphone');
}

export async function requestMicrophoneAccess(constraints = { audio: true }) {
  if (!navigator.mediaDevices?.getUserMedia) {
    const error = new Error('Microphone access is not supported in this browser.');
    error.name = 'NotSupportedError';
    throw error;
  }
  const permission = await microphonePermissionState();
  if (permission === 'denied') {
    const error = new Error('Microphone permission is blocked for this site.');
    error.name = 'NotAllowedError';
    throw error;
  }
  return navigator.mediaDevices.getUserMedia(constraints);
}

async function _mediaAccessMessage(error, kind) {
  const camera = kind === 'camera';
  const label = camera ? 'Camera' : 'Microphone';
  const lowerLabel = label.toLowerCase();
  if (!window.isSecureContext) {
    return `${label} access requires HTTPS or localhost.`;
  }
  if (window.top !== window.self) {
    return `${label} access is blocked in this embedded view. Open WebAgent directly in a browser tab.`;
  }
  if (!navigator.mediaDevices?.getUserMedia) {
    return `${label} access is not supported in this browser.`;
  }

  const permission = await _permissionState(lowerLabel);
  const name = String(error?.name || '');
  if (permission === 'denied') {
    return `${label} permission is blocked for this site. Use the address-bar site controls to allow it, then try again.`;
  }
  if (name === 'NotFoundError' || name === 'DevicesNotFoundError') {
    return camera
      ? 'No camera was found. Connect or enable a camera, then try again.'
      : 'No microphone was found. Connect or enable a microphone, then try again.';
  }
  if (name === 'NotReadableError' || name === 'TrackStartError') {
    return `Your ${lowerLabel} is unavailable or in use by another application.`;
  }
  if (name === 'SecurityError') {
    return `${label} access is blocked by this browser or device security policy.`;
  }
  if (name === 'NotAllowedError' || name === 'PermissionDeniedError') {
    return `Chrome did not allow ${lowerLabel} access. Check device privacy settings or open WebAgent directly in a browser tab.`;
  }
  return `${label} access could not be enabled.`;
}

export function microphoneAccessMessage(error) {
  return _mediaAccessMessage(error, 'microphone');
}

export function cameraAccessMessage(error) {
  return _mediaAccessMessage(error, 'camera');
}
