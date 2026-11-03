import {useLayoutEffect, useRef} from 'react';
import {reorderedIds} from './crowdChanges';

/** FLIP keyed rows only when the same songs actually change order. */
export function useQueueMotion(ids: string[]) {
  const root = useRef<HTMLDivElement>(null);
  const previous = useRef<{ids: string[]; tops: Map<string, number>}>({ids: [], tops: new Map()});
  const animations = useRef<Animation[]>([]);
  useLayoutEffect(() => {
    const container = root.current;
    if (!container) return;
    const rows = Array.from(container.querySelectorAll<HTMLElement>('[data-track-id]'));
    const tops = new Map(rows.map(row => [row.dataset.trackId!, row.offsetTop]));
    const moved = reorderedIds(previous.current.ids, ids);
    if (moved.length && !window.matchMedia('(prefers-reduced-motion: reduce)').matches) {
      animations.current.forEach(animation => animation.cancel());
      animations.current = rows.filter(row => moved.includes(row.dataset.trackId!)).map(row => {
        const delta = (previous.current.tops.get(row.dataset.trackId!) ?? row.offsetTop) - row.offsetTop;
        return row.animate([
          {transform: `translateY(${delta}px)`, backgroundColor: 'rgba(81, 191, 255, .18)'},
          {transform: 'translateY(0)', backgroundColor: 'transparent'},
        ], {duration: 850, easing: 'cubic-bezier(.2,.75,.2,1)'});
      });
    }
    previous.current = {ids: [...ids], tops};
  });
  useLayoutEffect(() => () => animations.current.forEach(animation => animation.cancel()), []);
  return root;
}
