import test from 'node:test';
import assert from 'node:assert/strict';
import {preparedPlaylistView} from '../src/preparedPlaylist.ts';

function fixture(ids = ['a','b','c']) {
  return {status:'ready',activationReady:true,buildId:'build-1',queueRevision:7,
    orderedTrackIds:ids,queue:ids.map(id=>({id,title:`Synthetic ${id}`})),
    activation:{status:'playlist_ready',queueRevision:8,builderRevision:7,
      orderedTrackIds:ids,trackCount:ids.length,playlist:'/synthetic/prepared-new.m3u8'}};
}

test('mounted status follows the validated export, not selected songs or an import preview', () => {
  const b=fixture();
  assert.equal(preparedPlaylistView(b).mounted,true);
  assert.equal(preparedPlaylistView(b).filename,'prepared-new.m3u8');
  b.activation=null;
  assert.equal(preparedPlaylistView(b).mounted,false);
  assert.equal(preparedPlaylistView(b).path,null);
});

test('seven-song rebuild cannot retain old three-song mount or preview identity', () => {
  const old=fixture();const next=fixture(['a','b','c','d','e','f','g']);
  next.buildId='build-2';next.queueRevision=8;next.activation=old.activation;
  const view=preparedPlaylistView(next);
  assert.equal(view.mounted,false);
  assert.notEqual(view.contextKey,preparedPlaylistView(old).contextKey);
  next.activation={...next.activation,builderRevision:8,orderedTrackIds:next.orderedTrackIds,trackCount:7};
  assert.equal(preparedPlaylistView(next).mounted,true);
});

test('same-count reorder and new export invalidate stale local preview state', () => {
  const b=fixture();const initial=preparedPlaylistView(b);
  b.orderedTrackIds=['b','a','c'];b.queue=b.orderedTrackIds.map(id=>({id,title:id}));
  assert.equal(preparedPlaylistView(b).mounted,false);
  assert.notEqual(preparedPlaylistView(b).contextKey,initial.contextKey);
  const beforeExport=preparedPlaylistView(b).contextKey;
  b.activation.playlist='/synthetic/new-export.m3u8';
  assert.notEqual(preparedPlaylistView(b).contextKey,beforeExport);
});

test('mounted import identifies the originating file and its exact order', () => {
  const b=fixture(['c','a']);b.selectionSource={kind:'mixxx_playlist',filename:'chosen.m3u8'};
  const view=preparedPlaylistView(b);
  assert.equal(view.source,'Imported from chosen.m3u8');
  assert.deepEqual(view.tracks.map(t=>t.id),['c','a']);
});
