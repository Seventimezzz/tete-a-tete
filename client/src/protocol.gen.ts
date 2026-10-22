// Сгенерировано из protocol/schema.json командой npm run gen:protocol. Не править руками.
// Источник истины — Pydantic-модели в server/app/protocol.py.
import { z } from 'zod';

export const CandidateType = z.enum(['host', 'srflx', 'prflx', 'upnp']);
export type CandidateType = z.infer<typeof CandidateType>;

export const ClientLeave = z.strictObject({ type: z.literal('leave') });
export type ClientLeave = z.infer<typeof ClientLeave>;

export const ClientMute = z.strictObject({ type: z.literal('mute'), muted: z.boolean() });
export type ClientMute = z.infer<typeof ClientMute>;

export const ClientPing = z.strictObject({ type: z.literal('ping') });
export type ClientPing = z.infer<typeof ClientPing>;

export const ErrorCode = z.enum(['room-full', 'bad-request']);
export type ErrorCode = z.infer<typeof ErrorCode>;

export const IpFamily = z.enum(['ipv4', 'ipv6']);
export type IpFamily = z.infer<typeof IpFamily>;

export const JsonValue = z.unknown();
export type JsonValue = z.infer<typeof JsonValue>;

export const Name = z
  .string()
  .regex(new RegExp('^[^\\x00-\\x1f\\x7f-\\x9f]+$', 'u'))
  .refine((value) => [...value].length >= 1)
  .refine((value) => [...value].length <= 32);
export type Name = z.infer<typeof Name>;

export const NatType = z.enum(['open', 'cone', 'symmetric', 'no-udp']);
export type NatType = z.infer<typeof NatType>;

export const NetInfo = z.strictObject({
  nat: NatType.nullable(),
  upnp: z.boolean().nullable(),
  cgnat: z.boolean().nullable(),
  doubleNat: z.boolean().nullable(),
  vpn: z.boolean().nullable(),
});
export type NetInfo = z.infer<typeof NetInfo>;

export const ClientCreate = z.strictObject({ type: z.literal('create'), name: Name, net: NetInfo });
export type ClientCreate = z.infer<typeof ClientCreate>;

export const PairResult = z.enum(['direct', 'failed']);
export type PairResult = z.infer<typeof PairResult>;

export const PeerId = z.string().regex(new RegExp('^[a-km-np-z2-9]{8}$', 'u'));
export type PeerId = z.infer<typeof PeerId>;

export const ClientSignal = z.strictObject({
  type: z.literal('signal'),
  to: PeerId,
  data: z.record(z.string(), JsonValue),
});
export type ClientSignal = z.infer<typeof ClientSignal>;

export const PeerInfo = z.object({ id: PeerId, name: Name, muted: z.boolean() });
export type PeerInfo = z.infer<typeof PeerInfo>;

export const ResumeToken = z
  .string()
  .regex(new RegExp('^[A-Za-z0-9_.-]+$', 'u'))
  .refine((value) => [...value].length <= 128);
export type ResumeToken = z.infer<typeof ResumeToken>;

export const RoomId = z.string().regex(new RegExp('^[a-km-np-z2-9]{12}$', 'u'));
export type RoomId = z.infer<typeof RoomId>;

export const ClientJoin = z.strictObject({
  type: z.literal('join'),
  roomId: RoomId,
  name: Name,
  net: NetInfo,
  resumeToken: ResumeToken.nullable().optional(),
});
export type ClientJoin = z.infer<typeof ClientJoin>;

export const Route = z.strictObject({ local: CandidateType, remote: CandidateType });
export type Route = z.infer<typeof Route>;

export const ClientPairReport = z.strictObject({
  type: z.literal('pair-report'),
  peer: PeerId,
  result: PairResult,
  route: Route.nullable(),
  family: IpFamily.nullable(),
  sameIp: z.boolean().nullable(),
  ms: z.number().int().min(0).max(3600000).nullable(),
  restarts: z.number().int().min(0).max(100),
});
export type ClientPairReport = z.infer<typeof ClientPairReport>;

export const ClientMessage = z.discriminatedUnion('type', [
  ClientCreate,
  ClientJoin,
  ClientSignal,
  ClientMute,
  ClientPairReport,
  ClientPing,
  ClientLeave,
]);
export type ClientMessage = z.infer<typeof ClientMessage>;

export const ServerError = z.object({ type: z.literal('error'), code: ErrorCode });
export type ServerError = z.infer<typeof ServerError>;

export const ServerJoined = z.object({
  type: z.literal('joined'),
  roomId: RoomId,
  selfId: PeerId,
  resumed: z.boolean(),
  resumeToken: ResumeToken,
  peers: z.array(PeerInfo).max(4),
});
export type ServerJoined = z.infer<typeof ServerJoined>;

export const ServerMute = z.object({ type: z.literal('mute'), id: PeerId, muted: z.boolean() });
export type ServerMute = z.infer<typeof ServerMute>;

export const ServerPeerJoined = z.object({
  type: z.literal('peer-joined'),
  id: PeerId,
  name: Name,
});
export type ServerPeerJoined = z.infer<typeof ServerPeerJoined>;

export const ServerPeerLeft = z.object({ type: z.literal('peer-left'), id: PeerId });
export type ServerPeerLeft = z.infer<typeof ServerPeerLeft>;

export const ServerPong = z.object({ type: z.literal('pong') });
export type ServerPong = z.infer<typeof ServerPong>;

export const ServerRelay = z.object({
  type: z.literal('relay'),
  a: PeerId,
  b: PeerId,
  via: PeerId.nullable(),
});
export type ServerRelay = z.infer<typeof ServerRelay>;

export const ServerSignal = z.object({
  type: z.literal('signal'),
  from: PeerId,
  data: z.record(z.string(), JsonValue),
});
export type ServerSignal = z.infer<typeof ServerSignal>;

export const ServerMessage = z.discriminatedUnion('type', [
  ServerJoined,
  ServerPeerJoined,
  ServerPeerLeft,
  ServerSignal,
  ServerMute,
  ServerRelay,
  ServerPong,
  ServerError,
]);
export type ServerMessage = z.infer<typeof ServerMessage>;
