export function fitDcvDisplay(
  containerWidth: number,
  containerHeight: number,
  remoteWidth: number,
  remoteHeight: number,
): { scale: number; offsetX: number; offsetY: number } {
  if (containerWidth <= 0 || containerHeight <= 0 || remoteWidth <= 0 || remoteHeight <= 0) {
    return { scale: 0, offsetX: 0, offsetY: 0 };
  }
  const scale = Math.min(containerWidth / remoteWidth, containerHeight / remoteHeight);
  return {
    scale,
    offsetX: (containerWidth - remoteWidth * scale) / 2,
    offsetY: (containerHeight - remoteHeight * scale) / 2,
  };
}
