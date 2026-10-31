import { useCallback, useEffect, useRef, useState } from 'react'

/**
 * Draggable divider between two stacked panes.
 *
 * Reports the drag as a fraction of the container height, so the caller can
 * shift weight from one pane to its neighbour. Keyboard accessible: focus it
 * and use the arrow keys.
 */
export default function Splitter({ containerRef, onResize, onReset, label }) {
  const [dragging, setDragging] = useState(false)
  const last = useRef(0)

  const height = () => containerRef.current?.clientHeight || 1

  const move = useCallback((e) => {
    const dy = e.clientY - last.current
    last.current = e.clientY
    if (dy) onResize(dy / height())
  }, [onResize, containerRef])

  const up = useCallback(() => {
    setDragging(false)
    document.body.classList.remove('row-resizing')
  }, [])

  useEffect(() => {
    if (!dragging) return
    window.addEventListener('pointermove', move)
    window.addEventListener('pointerup', up)
    return () => {
      window.removeEventListener('pointermove', move)
      window.removeEventListener('pointerup', up)
    }
  }, [dragging, move, up])

  return (
    <div
      className={`splitter ${dragging ? 'dragging' : ''}`}
      role="separator"
      aria-orientation="horizontal"
      aria-label={label || 'Resize panels'}
      tabIndex={0}
      onPointerDown={(e) => {
        e.preventDefault()
        last.current = e.clientY
        setDragging(true)
        document.body.classList.add('row-resizing')
      }}
      onDoubleClick={onReset}
      title="Drag to resize · double-click to reset"
      onKeyDown={(e) => {
        const step = e.shiftKey ? 48 : 16
        if (e.key === 'ArrowUp') { e.preventDefault(); onResize(-step / height()) }
        if (e.key === 'ArrowDown') { e.preventDefault(); onResize(step / height()) }
        if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onReset?.() }
      }}
    />
  )
}
