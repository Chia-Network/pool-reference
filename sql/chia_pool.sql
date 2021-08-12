/*
Navicat MySQL Data Transfer

Source Server         : chia矿池本地数据库
Source Server Version : 50734
Source Host           : 192.168.135.153:3306
Source Database       : chia_pool

Target Server Type    : MYSQL
Target Server Version : 50734
File Encoding         : 65001

Date: 2021-08-12 14:22:17
*/

SET FOREIGN_KEY_CHECKS=0;

-- ----------------------------
-- Table structure for farmer
-- ----------------------------
DROP TABLE IF EXISTS `farmer`;
CREATE TABLE `farmer` (
  `launcher_id` varchar(500) NOT NULL,
  `p2_singleton_puzzle_hash` text,
  `delay_time` bigint(20) DEFAULT NULL,
  `delay_puzzle_hash` text,
  `authentication_public_key` text,
  `singleton_tip` blob,
  `singleton_tip_state` blob,
  `points` bigint(20) DEFAULT NULL,
  `difficulty` bigint(20) DEFAULT NULL,
  `payout_instructions` text,
  `is_pool_member` tinyint(4) DEFAULT NULL,
  PRIMARY KEY (`launcher_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- ----------------------------
-- Table structure for partial
-- ----------------------------
DROP TABLE IF EXISTS `partial`;
CREATE TABLE `partial` (
  `launcher_id` text,
  `timestamp` bigint(20) DEFAULT NULL,
  `difficulty` bigint(20) DEFAULT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
